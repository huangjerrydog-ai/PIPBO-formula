import copy
import sqlite3
import unittest
import test_workflow as fixtures
from fastapi.testclient import TestClient
from copolymer import solve_molar
from engine import RuleError
server=fixtures.server

class Supplement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(server.app);cls.client.headers['X-Pipbo']='1'
        cls.client.post('/api/login',json={'name':'director','password':'testpass-1234'})
        with server.connect() as c:cls.cat=server.catalog(c)
    def design(self):
        return dict(route='N',precursor_type='PAE_3STEP',r_ratio=.98,base_group_b_mmol=100,
          group_a=[dict(code='DA-03',ratio=7),dict(code='DA-06',ratio=3)],group_b=[dict(code='DM-01',ratio=8),dict(code='DM-06',ratio=2)],
          pae_reagent_equivs=dict(hema_eq=2.05,dbu_mol_pct=5,socl2_eq=2.1,py_eq=4.2,hq1_wt_pct=.1,hq2_mass_g=.01,nmp1_g=70,nmp2_g=30))
    def test_01_mass_inverse_and_normalization(self):
        d=self.design();r=solve_molar(d,self.cat)
        self.assertAlmostEqual(r['theoretical_resin_g'],78.300308)
        self.assertAlmostEqual(r['r_eff'],.98/1.02);self.assertAlmostEqual(r['Xn_p1'],99)
        self.assertEqual(len(r['steps']),4)
        cl=next(x for x in r['molar_items'] if x['code']=='CL-01')
        self.assertAlmostEqual(cl['mass'],.098*2.1*118.97)
        self.assertAlmostEqual(cl['volume_ml'],cl['mass']/1.631)
        d.update(drive_mode='TARGET_DRY',target_dry_g=100);d['group_a'][0]['purity']=95
        r=solve_molar(d,self.cat);self.assertAlmostEqual(r['theoretical_resin_g'],100)
        x=r['molar_items'][0];self.assertAlmostEqual(x['mass']*.95,x['moles']*self.cat[x['code']]['mw'])
        d['fraction_mode']='PERCENT'
        with self.assertRaises(RuleError):solve_molar(d,self.cat)
        d['fraction_mode']='RATIO';d['pae_reagent_equivs']['py_eq']=1
        with self.assertRaises(RuleError):solve_molar(d,self.cat)
    def test_02_pbo_salt_and_missing(self):
        d=dict(route='P',r_ratio=.98,base_group_b_mmol=100,group_a=[dict(code='PBO-DC01',ratio=1)],group_b=[dict(code='PBO-AP07',ratio=1)])
        r=solve_molar(d,self.cat)
        self.assertAlmostEqual(r['DAR_extra_base_mol'],.205)
        self.assertAlmostEqual(r['py_required_mol'],.196+.205)
        self.assertAlmostEqual(r['molar_items'][1]['mass'],21.306)
        self.assertFalse(r['process_required']);self.assertTrue(all(x['stage']==1 for x in r['molar_items']))
        d=self.design();d['pae_reagent_equivs']={}
        self.assertEqual(len(solve_molar(d,self.cat)['configuration_missing']),3)
        self.assertEqual(self.client.post('/api/lots/molar',json=d).status_code,422)
    def test_03_four_stage_gates_and_release(self):
        d=self.design();res=self.client.post('/api/lots/molar',json=d)
        self.assertEqual(res.status_code,200,res.text);ident=res.json()['id'];url=f'/api/lots/{ident}'
        self.assertEqual(self.client.post(url+'/transition',json={'status':'QC_PENDING'}).status_code,422)
        with self.assertRaises(sqlite3.IntegrityError):
            with server.connect(True) as c:c.execute("UPDATE lots SET status='QC_PENDING' WHERE id=?",(ident,))
        records=[dict(actual_temp_min_c=20,actual_temp_max_c=25,nitrogen_ready=True),
                 dict(actual_temp_min_c=0,actual_temp_max_c=10,cooling_medium='ICE_WATER_BATH',dosing_method='DROPWISE_FUNNEL',tailgas_ready=True,dropwise_hours=1),
                 dict(actual_temp_min_c=0,actual_temp_max_c=10,cooling_medium='ICE_WATER_BATH',dosing_method='PREMIX_SOLUTION',dropwise_hours=1,post_rt_hours=2),
                 dict(actual_temp_min_c=40,actual_temp_max_c=60,vacuum_ready=True,wash_cycles_count=2,wash_water_ml=500,wash_ethanol_ml=200,precipitation_confirmed=True,actual_dry_yield_g=70)]
        self.assertEqual(self.client.post(url+'/stages/2/start',json=records[1]).status_code,422)
        for n,record in enumerate(records,1):
            record.update(apparatus_config='测试装置',weighing_verified=True,hold_hours=24 if n in (1,4) else 2)
            if n in (2,3,4):
                bad=dict(record,actual_temp_max_c=61 if n==4 else 11)
                self.assertEqual(self.client.post(url+f'/stages/{n}/start',json=bad).status_code,422)
            self.assertEqual(self.client.post(url+f'/stages/{n}/start',json=record).status_code,200)
            bad=dict(record,hold_hours=1)
            self.assertEqual(self.client.post(url+f'/stages/{n}/complete',json=bad).status_code,422)
            if n==4:
                self.assertEqual(self.client.post(url+'/stages/4/complete',json=dict(record,wash_cycles_count=0)).status_code,422)
            done=self.client.post(url+f'/stages/{n}/complete',json=record)
            self.assertEqual(done.status_code,200,done.text)
        state=self.client.get(url+'/process').json();self.assertEqual(state['status'],'QC_PENDING')
        self.assertAlmostEqual(state['workup']['yield_pct'],70/78.300308*100)
        with self.assertRaises(sqlite3.IntegrityError):
            with server.connect(True) as c:c.execute("UPDATE process_stages SET status='PENDING' WHERE lot_id=? AND stage_no=1",(ident,))
        qc=dict(status='RELEASED',sc=99.5,mn=12000,mw=24000,viscosity=300,water_ppm=120,stock=71,form='DRY_POWDER',solvent='SV-01')
        self.assertEqual(self.client.post(url+'/transition',json=qc).status_code,422)
        qc['stock']=70;qc['form']='MOTHER_LIQUOR'
        self.assertEqual(self.client.post(url+'/transition',json=qc).status_code,422)
        qc['form']='DRY_POWDER'
        res=self.client.post(url+'/transition',json=qc);self.assertEqual(res.status_code,200,res.text)
        self.assertEqual(self.client.post(url+'/stages/4/complete',json=records[3]).status_code,409)
        lab=TestClient(server.app);lab.headers['X-Pipbo']='1';lab.post('/api/login',json={'name':'lab','password':'lab-test-1234'})
        self.assertEqual(lab.get(url+'/process').status_code,403)
        self.assertEqual(lab.post('/api/synthesis/molar/preview',json=d).status_code,403)

    def test_04_variable_stage_additives(self):
        d=self.design();d['stage_additives']=[
            dict(stage=1,code='CAT-01',mode='MOL_PERCENT',value=5,purity=95),
            dict(stage=1,code='IN-02',mode='MASS',value=.03,purity=100),
            dict(stage=3,code='AS-01',mode='AUTO_PY',value='',purity=90),
            dict(stage=4,code='WP-01',mode='MASS',value=50,purity=100)]
        r=solve_molar(d,self.cat)
        self.assertEqual(r['configuration_missing'],[])
        self.assertFalse(any(x['code']=='IN-03' for x in r['molar_items']))
        self.assertAlmostEqual(next(x['mass'] for x in r['molar_items'] if x['code']=='CAT-01'),.098*.05*152.24/.95)
        self.assertAlmostEqual(r['py_feed_mol'],r['py_required_mol'])
        self.assertEqual(r['steps'][3]['inputs'][0]['code'],'WP-01')
        res=self.client.post('/api/lots/molar',json=d);self.assertEqual(res.status_code,200,res.text)
        process=self.client.get('/api/lots/'+res.json()['id']+'/process').json()
        self.assertEqual(process['stages'][3]['template']['inputs'][0]['mass'],50)
        d['stage_additives']=[dict(stage=3,code='AS-01',mode='MASS',value=1,purity=100)]
        self.assertTrue(solve_molar(d,self.cat)['configuration_missing'])
        self.assertEqual(self.client.post('/api/lots/molar',json=d).status_code,422)
        d['stage_additives']=[dict(stage=1,code='DA-01',mode='MASS',value=1)]
        with self.assertRaises(RuleError):solve_molar(d,self.cat)
        d['stage_additives']=[dict(stage=3,code='AS-01',mode='AUTO_PY',value=''),dict(stage=3,code='AS-01',mode='MASS',value=1)]
        with self.assertRaises(RuleError):solve_molar(d,self.cat)
        d['stage_additives']=[dict(stage=1,code='IN-01',mode='AUTO_PY',value='')]
        with self.assertRaises(RuleError):solve_molar(d,self.cat)
