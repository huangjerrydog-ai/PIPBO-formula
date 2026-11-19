import copy
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Never touch the user's actual database.
SANDBOX=tempfile.TemporaryDirectory(prefix='pipbo-test-')
os.environ['PIPBO_DATA']=SANDBOX.name
from fastapi.testclient import TestClient
import server
from engine import RuleError, formulation, synthesis

class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(server.app)
        cls.client.headers['X-Pipbo']='1'
        assert cls.client.post('/api/setup',json={'name':'director','password':'testpass-1234'}).status_code==200
        assert cls.client.post('/api/login',json={'name':'director','password':'testpass-1234'}).status_code==200
        with server.connect() as c: cls.cat=server.catalog(c)

    def make_lot(self,stock=850):
        data={'route':'P','conversion':.995,'monomers':[{'code':'PBO-DC01','mass':20.302,'purity':100},{'code':'PBO-AP01','mass':38.4573,'purity':100}]}
        r=self.client.post('/api/lots',json=data); self.assertEqual(r.status_code,200,r.text); ident=r.json()['id']
        self.assertEqual(self.client.post(f'/api/lots/{ident}/transition',json={'status':'QC_PENDING'}).status_code,200)
        qc={'status':'RELEASED','sc':28.5,'mn':12000,'mw':24000,'viscosity':300,'water_ppm':120,'stock':stock,'form':'MOTHER_LIQUOR','solvent':'SV-02','expiry':'2099-12-31'}
        r=self.client.post(f'/api/lots/{ident}/transition',json=qc); self.assertEqual(r.status_code,200,r.text)
        return ident

    def formula(self,ident):
        return {'route':'P','resin_g':100,'target_sc':25,'basis':'TOTAL','lots':[{'id':ident,'ratio':100}],
                'additives':[{'code':'PAC-01','phr':18,'sc':100},{'code':'AD-01','phr':2.5,'sc':100},{'code':'AD-02','phr':.5,'sc':100}],
                'solvents':[{'code':'SV-02','ratio':80},{'code':'SV-03','ratio':20}]}

    def test_01_document_golden_and_idempotency(self):
        ident=self.make_lot(); d=self.formula(ident)
        r=self.client.post('/api/formulations/preview',json=d); self.assertEqual(r.status_code,200,r.text); result=r.json()
        self.assertAlmostEqual(result['total_g'],484)
        self.assertAlmostEqual(result['lines'][0]['mass'],350.87719298245617)
        solvent={x['code']:x['mass'] for x in result['lines'] if x['kind']=='SOLVENT'}
        self.assertAlmostEqual(solvent['SV-02'],39.52280701754384)
        self.assertAlmostEqual(solvent['SV-03'],72.6)
        d['request_key']='golden-example-0001'
        saved=self.client.post('/api/formulations',json=d); self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.client.post('/api/formulations',json=d).json(),saved.json())
        lot=next(x for x in self.client.get('/api/lots').json() if x['id']==ident)
        self.assertAlmostEqual(lot['stock'],499.12280701754383)
        d['resin_g']=110
        self.assertEqual(self.client.post('/api/formulations',json=d).status_code,409)
        csv=self.client.get('/api/formulations/'+saved.json()['id']+'/csv'); self.assertEqual(csv.status_code,200)
        self.assertIn('PAC-01',csv.text)
        self.assertEqual(self.client.post('/api/measurements',json={'formula_id':saved.json()['id'],'film_um':8,'residual_pct':95}).status_code,200)

    def test_02_guards_and_atomic_rollback(self):
        ident=self.make_lot(); d=self.formula(ident)
        with server.connect() as c: before=c.execute('SELECT stock FROM lots WHERE id=?',(ident,)).fetchone()[0]
        for alter in ('wrongroute','badphoto','negative','ratio','expiry','stock'):
            bad=copy.deepcopy(d)
            if alter=='wrongroute': bad['route']='N'
            if alter=='badphoto': bad['additives'][0]['code']='PI-01'
            if alter=='negative': bad['solvents']=[{'code':'SV-02','ratio':10},{'code':'SV-03','ratio':90}]
            if alter=='ratio': bad['lots'][0]['ratio']=50
            if alter=='stock': bad['resin_g']=1000
            if alter=='expiry':
                with server.connect(True) as c: c.execute('UPDATE lots SET expiry=? WHERE id=?',('2000-01-01',ident))
            bad['request_key']='bad-'+alter+'-000000000000'
            self.assertEqual(self.client.post('/api/formulations',json=bad).status_code,422,alter)
            if alter=='expiry':
                with server.connect(True) as c: c.execute('UPDATE lots SET expiry=? WHERE id=?',('2099-12-31',ident))
        with server.connect() as c: self.assertEqual(c.execute('SELECT stock FROM lots WHERE id=?',(ident,)).fetchone()[0],before)
        # A valid first lot followed by an insufficient second lot cannot partially deduct.
        small=self.make_lot(1)
        d['lots']=[{'id':ident,'ratio':50},{'id':small,'ratio':50}];d['request_key']='rollback-test-0000001'
        self.assertEqual(self.client.post('/api/formulations',json=d).status_code,422)
        with server.connect() as c: self.assertEqual(c.execute('SELECT stock FROM lots WHERE id=?',(ident,)).fetchone()[0],before)

    def test_03_concurrent_stock(self):
        ident=self.make_lot(400); payload=self.formula(ident)
        def save(i):
            d=copy.deepcopy(payload);d['request_key']=f'concurrent-request-{i}'
            return self.client.post('/api/formulations',json=d).status_code
        with ThreadPoolExecutor(2) as pool: statuses=list(pool.map(save,[1,2]))
        self.assertEqual(sorted(statuses),[200,422])
        with server.connect() as c: stock=c.execute('SELECT stock FROM lots WHERE id=?',(ident,)).fetchone()[0]
        self.assertAlmostEqual(stock,49.12280701754383)

    def test_04_masking_and_auth(self):
        self.assertEqual(self.client.post('/api/users',json={'name':'lab','password':'lab-test-1234','role':'LAB'}).status_code,200)
        lab=TestClient(server.app);lab.headers['X-Pipbo']='1';self.assertEqual(lab.post('/api/login',json={'name':'lab','password':'lab-test-1234'}).status_code,200)
        m=lab.get('/api/materials').json();self.assertNotIn('cas',m[0]);self.assertNotIn('name',m[0])
        for lot in lab.get('/api/lots').json(): self.assertNotIn('monomers',lot);self.assertNotIn('note',lot)
        self.assertEqual(lab.post('/api/lots',json={}).status_code,403)
        self.assertEqual(lab.get('/api/audit').status_code,403)
        self.assertEqual(TestClient(server.app).get('/api/lots').status_code,401)
        self.assertEqual(self.client.post('/api/users',json={},headers={'Origin':'http://evil.example'}).status_code,403)
        with server.connect() as c:
            row=c.execute("SELECT secret FROM materials WHERE code='PBO-AP01'").fetchone()
            self.assertNotIn(b'BisAPAF',row[0]);self.assertEqual(server.decrypt(row[0])['name'],'BisAPAF')

    def test_05_synthesis_and_numeric_edges(self):
        self.assertEqual(len(self.cat),72)
        d={'route':'P','conversion':1,'monomers':[{'code':'PBO-DC01','mass':20.302},{'code':'PBO-AP07','mass':23.4366}]}
        r=synthesis(d,self.cat)
        self.assertAlmostEqual(r['nA'],.1);self.assertAlmostEqual(r['nB'],.11)
        self.assertAlmostEqual(r['DAR_extra_base_mol'],.2255)
        self.assertAlmostEqual(r['theoretical_resin_g'],20.302+.11*140.14-.2*36.46)
        d['monomers'][1]={'code':'PBO-AP01','mass':36.626};self.assertIsNone(synthesis(d,self.cat)['Xn'])
        d['conversion']=float('nan')
        with self.assertRaises(RuleError): synthesis(d,self.cat)
        # Dry powder residual solvent and additive carrier must both be counted.
        ident=self.make_lot();p=self.formula(ident);p['additives'][0].update(sc=50,solvent='SV-03')
        r=self.client.post('/api/formulations/preview',json=p);self.assertEqual(r.status_code,200,r.text)
        self.assertAlmostEqual(sum(x['mass'] for x in r.json()['lines']),484)

    def test_06_mixed_carrier_powder_and_release_lock(self):
        ident=self.make_lot()
        with server.connect(True) as c:
            row=c.execute('SELECT qc FROM lots WHERE id=?',(ident,)).fetchone()
            qc=server.json.loads(row['qc']);qc['solvent_profile']=[{'code':'SV-02','ratio':80},{'code':'SV-03','ratio':20}]
            c.execute('UPDATE lots SET qc=? WHERE id=?',(server.pack(qc),ident))
        d=self.formula(ident)
        r=self.client.post('/api/formulations/preview',json=d);self.assertEqual(r.status_code,200,r.text)
        carrier=r.json()['carried_solvents']
        self.assertAlmostEqual(carrier['SV-02'],250.87719298245617*.8)
        self.assertAlmostEqual(carrier['SV-03'],250.87719298245617*.2)
        self.assertEqual(self.client.post(f'/api/lots/{ident}/transition',json={'status':'RELEASED'}).status_code,422)
        # Powder still carries its measured residual solvent, not assumed 100% dry.
        with server.connect(True) as c:
            qc['sc']=99.5;qc['form']='DRY_POWDER'
            c.execute('UPDATE lots SET qc=? WHERE id=?',(server.pack(qc),ident))
        r=self.client.post('/api/formulations/preview',json=d);self.assertEqual(r.status_code,200,r.text)
        self.assertAlmostEqual(r.json()['lines'][0]['mass'],100/.995)
        self.assertAlmostEqual(sum(r.json()['carried_solvents'].values()),100/.995-100)
        self.assertEqual(self.client.post(f'/api/lots/{ident}/transition',json={'status':'QUARANTINED'}).status_code,200)
        self.assertEqual(self.client.post('/api/formulations/preview',json=d).status_code,422)

if __name__=='__main__': unittest.main()
