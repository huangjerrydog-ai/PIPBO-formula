import copy
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from engine import RuleError,balance_feed,synthesis
from catalog import seed_rows
import test_workflow as fixtures
server=fixtures.server


class SynthesisRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(server.app);cls.client.headers['X-Pipbo']='1'
        if cls.client.get('/api/status').json()['setup']:
            cls.client.post('/api/setup',json={'name':'director','password':'testpass-1234'})
        assert cls.client.post('/api/login',json={'name':'director','password':'testpass-1234'}).status_code==200
        with server.connect() as c:cls.cat=server.catalog(c)
        cls.client.post('/api/users',json={'name':'cn-lab','password':'cn-tests-1234','role':'LAB'})
        cls.lab=TestClient(server.app);cls.lab.headers['X-Pipbo']='1'
        cls.lab.post('/api/login',json={'name':'cn-lab','password':'cn-tests-1234'})

    def base(self):
        return dict(route='N',synthesis_path='PAE_ACYL_CHLORIDE',conversion=.995,monomers=[dict(code='DA-01',mass=21.812,purity=100),dict(code='DM-01',mass=20.024,purity=100)],graft_code='GR-01',graft_degree=1,acyl_eq=1,acyl_purity=100,sidechain_purity=100,sidechain_excess_pct=0)

    def test_01_chinese_names_encrypted_edit_and_permissions(self):
        seeded=list(seed_rows());self.assertEqual(len(seeded),72)
        self.assertTrue(all(m['identity']['cn_name'] for m in seeded))
        self.assertEqual(self.cat['RG-01']['mw'],118.96)
        row=next(m for m in self.client.get('/api/materials').json() if m['code']=='DA-01')
        self.assertEqual(row['cn_name'],'均苯四甲酸二酐')
        r=self.client.post('/api/materials/DA-01/names',json={'cn_name':'均苯四甲酸二酐（名称编辑测试）','name':'PMDA'})
        self.assertEqual(r.status_code,200,r.text)
        with server.connect() as c:
            record=c.execute("SELECT * FROM materials WHERE code='DA-01'").fetchone()
            self.assertEqual(record['mw'],218.12)
            self.assertNotIn('名称编辑测试'.encode(),record['secret'])
            self.assertEqual(server.decrypt(record['secret'])['cas'],'89-32-7')
        self.assertEqual(self.lab.post('/api/materials/DA-01/names',json={'cn_name':'bad'}).status_code,403)
        self.assertTrue(all('cn_name' not in m and 'name' not in m for m in self.lab.get('/api/materials').json()))
        self.assertEqual(self.client.post('/api/materials/DA-01/names',json={'cn_name':''}).status_code,422)
        self.client.post('/api/materials/DA-01/names',json={'cn_name':'均苯四甲酸二酐'})

    def test_02_excess_selection_balances_purity_and_blends(self):
        d=self.base();d.update(synthesis_path='PAA_DIRECT',graft_code='',stoichiometry_mode='B_EXCESS',excess_pct=5)
        d['monomers']=[{'code':'DA-01','mass':10,'purity':98},{'code':'DA-02','mass':20,'purity':99},{'code':'DM-01','mass':15,'purity':97},{'code':'DM-02','mass':30,'purity':96},{'code':'EC-01','mass':.5,'purity':100}]
        with self.assertRaises(RuleError):synthesis(d,self.cat)
        adjusted=balance_feed(d,self.cat);self.assertEqual(adjusted['adjusted_group'],'DM')
        self.assertAlmostEqual(adjusted['monomers'][2]['mass']/adjusted['monomers'][3]['mass'],.5)
        self.assertEqual(adjusted['monomers'][-1]['mass'],.5)
        d['monomers']=adjusted['monomers'];result=synthesis(d,self.cat)
        self.assertAlmostEqual(result['nB']/result['nA'],1.05);self.assertAlmostEqual(result['actual_excess_pct'],5)
        self.assertAlmostEqual(result['endcap_required_mol'],2*(result['nB']-result['nA']))
        for mode in ('A_EXCESS','BALANCED'):
            d['stoichiometry_mode']=mode;d['monomers']=balance_feed(d,self.cat)['monomers'];r=synthesis(d,self.cat)
            self.assertEqual(r['actual_excess_side'],mode)
            if mode=='A_EXCESS':self.assertAlmostEqual(r['nA']/r['nB'],1.05);self.assertEqual(r['endcap_required_mol'],0)
            else:self.assertAlmostEqual(r['nA'],r['nB'])
        d['route']='P';d['synthesis_path']='PHA_DIRECT';d['monomers']=[{'code':'PBO-DC01','mass':20.302},{'code':'PBO-AP01','mass':36.626}];d['stoichiometry_mode']='B_EXCESS'
        d['monomers']=balance_feed(d,self.cat)['monomers'];self.assertAlmostEqual(synthesis(d,self.cat)['nB']/synthesis(d,self.cat)['nA'],1.05)

    def test_03_pae_three_stage_golden_and_mass_balance(self):
        d=self.base();r=synthesis(d,self.cat)
        self.assertEqual(len(r['steps']),3)
        self.assertAlmostEqual(r['graft_moles'],.2);self.assertAlmostEqual(r['graft_feed_g'],26.028)
        self.assertAlmostEqual(r['acyl_feed_g'],23.792);self.assertAlmostEqual(r['theoretical_resin_g'],64.26)
        self.assertAlmostEqual(r['steps'][0]['theoretical_intermediate_g'],47.84)
        self.assertAlmostEqual(r['steps'][1]['theoretical_intermediate_g'],51.528)
        self.assertAlmostEqual(r['HCl_generated_g'],14.584)
        gas=sum(s['HCl_g']+s['SO2_g'] for s in r['steps'])
        pure_feed=sum(x['mass'] for x in d['monomers'])+r['graft_feed_g']+r['acyl_feed_g']
        self.assertAlmostEqual(pure_feed-gas,r['theoretical_resin_g'])
        d['graft_code']='GR-04';r=synthesis(d,self.cat)
        self.assertEqual(r['sidechain'],'丙烯酸酯侧链');self.assertAlmostEqual(r['graft_feed_g'],23.224)
        self.assertAlmostEqual(r['theoretical_resin_g'],61.456)

    def test_04_excess_reagents_and_purity_do_not_inflate_polymer(self):
        d=self.base();d.update(sidechain_excess_pct=10,sidechain_purity=98,acyl_eq=1.2,acyl_purity=99)
        r=synthesis(d,self.cat)
        self.assertAlmostEqual(r['graft_feed_g'],.2*1.1*130.14/.98)
        self.assertAlmostEqual(r['acyl_feed_g'],.2*1.2*118.96/.99)
        self.assertAlmostEqual(r['steps'][0]['actual_excess_pct'],10)
        self.assertAlmostEqual(r['steps'][1]['actual_equivalents'],1.2)
        self.assertAlmostEqual(r['theoretical_resin_g'],64.26)
        d.update(sidechain_mass=31,acyl_mass=35);r=synthesis(d,self.cat)
        self.assertEqual(r['graft_feed_g'],31);self.assertEqual(r['acyl_feed_g'],35)
        self.assertAlmostEqual(r['theoretical_resin_g'],64.26)

    def test_05_incompatible_paths_and_insufficient_reagents(self):
        for fields in ({'graft_code':'GR-02'},{'graft_code':'GR-03'},{'graft_degree':.5},{'acyl_eq':.9},{'acyl_mass':1},{'sidechain_mass':1},{'acyl_purity':0},{'synthesis_path':'PHA_DIRECT'},{'synthesis_path':'PAA_DIRECT'},{'synthesis_path':'POST_GRAFT','graft_code':''}):
            d=self.base();d.update(fields)
            with self.subTest(fields=fields),self.assertRaises(RuleError):synthesis(d,self.cat)
        d=self.base();d.update(stoichiometry_mode='B_EXCESS',excess_pct=-1)
        with self.assertRaises(RuleError):balance_feed(d,self.cat)
        d=self.base();d['graft_code']='GR-02';d['synthesis_path']='POST_GRAFT';d['graft_degree']=.5
        r=synthesis(d,self.cat);self.assertEqual(r['steps'],[]);self.assertAlmostEqual(r['graft_feed_g'],.1*142.15)
        d=self.base();d.pop('synthesis_path');r=synthesis(d,self.cat)
        self.assertEqual(r['synthesis_path'],'POST_GRAFT')  # Existing records keep legacy semantics.

    def test_06_lot_persistence_and_lab_masking(self):
        d=self.base();d.update(stoichiometry_mode='B_EXCESS',excess_pct=5,step1_note='phase-one',step2_note='phase-two',step3_note='phase-three')
        adjust=self.client.post('/api/synthesis/balance',json=d);self.assertEqual(adjust.status_code,200,adjust.text)
        d['monomers']=adjust.json()['monomers']
        preview=self.client.post('/api/synthesis/preview',json=d);self.assertEqual(preview.status_code,200,preview.text)
        created=self.client.post('/api/lots',json=d);self.assertEqual(created.status_code,200,created.text)
        ident=created.json()['id'];saved=next(x for x in self.client.get('/api/lots').json() if x['id']==ident)
        self.assertEqual(saved['synthesis_path'],'PAE_ACYL_CHLORIDE');self.assertEqual(saved['step2_note'],'phase-two')
        self.assertEqual(saved['estimate']['steps'][2]['note'],'phase-three')
        self.assertAlmostEqual(saved['estimate']['actual_excess_pct'],5)
        masked=next(x for x in self.lab.get('/api/lots').json() if x['id']==ident)
        for key in ('estimate','graft_code','step1_note','synthesis_path'):self.assertNotIn(key,masked)
        self.assertEqual(self.lab.post('/api/synthesis/balance',json=d).status_code,403)

    def test_07_chinese_name_csv_and_old_templates(self):
        stream=io.StringIO();w=csv.writer(stream)
        w.writerow(['内部编码','物料名称','中文名称','类别','适用路线','分子量']);w.writerow(['CN-CSV-NEW','test-reagent','中文工艺试剂','RG','BOTH',123])
        r=self.client.post('/api/imports/materials/preview',content=stream.getvalue().encode('utf-8-sig'),headers={'X-File-Name':'names.csv'})
        self.assertEqual(r.status_code,200,r.text)
        job=r.json()['job_id'];self.assertEqual(self.client.post('/api/imports/materials/commit',json={'job_id':job}).status_code,200)
        with server.connect() as c:
            row=c.execute("SELECT secret FROM materials WHERE code='CN-CSV-NEW'").fetchone()
            self.assertEqual(server.decrypt(row['secret'])['cn_name'],'中文工艺试剂')
        old={'code':'CN-LEGACY-NEW','name':'中文旧模板名称','category':'SV','route':'BOTH','mw':100}
        self.assertEqual(self.client.post('/api/materials',json=old).status_code,200)
        self.assertEqual(next(x for x in self.client.get('/api/materials').json() if x['code']==old['code'])['cn_name'],old['name'])

    def test_08_migrate_seeded_names_preserves_existing_identity(self):
        with tempfile.TemporaryDirectory(prefix='pipbo-cn-migration-') as path:
            env=dict(os.environ,PIPBO_DATA=path,PYTHONDONTWRITEBYTECODE='1')
            init="""import server
with server.connect(True) as c:
 r=c.execute("SELECT secret FROM materials WHERE code='DA-01'").fetchone()
 d=server.decrypt(r['secret']);d.pop('cn_name',None);d['supplier']='KEEP-SUPPLIER';d['grade']='KEEP-GRADE';d['name']='CUSTOM-PMDA'
 c.execute("UPDATE materials SET secret=? WHERE code='DA-01'",(server.encrypt(d),))
 custom={'name':'自定义旧物料','cas':'CUSTOM','supplier':'KEEP-CUSTOM','grade':''}
 c.execute('INSERT INTO materials VALUES(?,?,?,?,?,?)',('CN-CUSTOM-OLD','SV','BOTH',100,server.encrypt(custom),'old-import'))
"""
            result=subprocess.run([sys.executable,'-c',init],env=env,cwd=server.ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            check="""import server,json
with server.connect() as c:
 r=c.execute("SELECT secret FROM materials WHERE code='DA-01'").fetchone();d=server.decrypt(r['secret']);custom=server.decrypt(c.execute("SELECT secret FROM materials WHERE code='CN-CUSTOM-OLD'").fetchone()['secret']);print(json.dumps({'seed':d,'custom':custom},ensure_ascii=False))
"""
            result=subprocess.run([sys.executable,'-c',check],env=env,cwd=server.ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);payload=json.loads(result.stdout);identity=payload['seed']
            self.assertEqual(payload['custom']['cn_name'],'自定义旧物料');self.assertEqual(payload['custom']['supplier'],'KEEP-CUSTOM')
            self.assertEqual(identity['cn_name'],'均苯四甲酸二酐');self.assertEqual(identity['supplier'],'KEEP-SUPPLIER')
            self.assertEqual(identity['grade'],'KEEP-GRADE');self.assertEqual(identity['name'],'CUSTOM-PMDA')

if __name__=='__main__':unittest.main()
