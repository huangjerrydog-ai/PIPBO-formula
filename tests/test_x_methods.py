import copy
import sqlite3
import unittest
import test_workflow as fixtures
from fastapi.testclient import TestClient
server=fixtures.server

class ReactionMethods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c=TestClient(server.app);cls.c.headers['X-Pipbo']='1'
        assert cls.c.post('/api/login',json={'name':'director','password':'testpass-1234'}).status_code==200
    def method(self):
        return dict(name='自定义聚合试验',route='N',description='人工称量模板',stages=[
            dict(name='聚合',temp_min_c=-5,temp_max_c=10,hold_hours=2,apparatus='冰浴',note='核对装置',inputs=[dict(code='DA-03',mass=10,purity=99),dict(code='DM-01',mass=8,purity=100)]),
            dict(name='后处理',temp_min_c=40,temp_max_c=60,hold_hours=24,inputs=[dict(code='WP-01',mass=50,purity=100)])])
    def save(self):
        d=self.method();r=self.c.post('/api/reaction-methods',json=d);self.assertEqual(r.status_code,200,r.text);return dict(d,**r.json())
    def test_01_versions_disable_and_permissions(self):
        m=self.save();ident=m['id'];old=copy.deepcopy(m)
        m['name']='已修改方法';r=self.c.post('/api/reaction-methods',json=m);self.assertEqual(r.json()['version'],2)
        self.assertEqual(self.c.post('/api/reaction-methods',json=old).status_code,409)
        self.assertEqual(self.c.post('/api/reaction-methods/'+ident+'/preview',json=old).status_code,409)
        m['version']=2
        self.assertEqual(self.c.post('/api/reaction-methods/'+ident+'/enabled',json={'enabled':False}).status_code,200)
        self.assertEqual(self.c.post('/api/reaction-methods/'+ident+'/batches',json=m).status_code,422)
        lab=TestClient(server.app);lab.headers['X-Pipbo']='1';lab.post('/api/login',json={'name':'lab','password':'lab-test-1234'})
        self.assertEqual(lab.get('/api/reaction-methods').status_code,403)
        self.assertEqual(lab.post('/api/reaction-methods',json=m).status_code,403)
        bad=self.method();bad['stages'][0]['inputs'][0]['code']='PBO-AP01'
        self.assertEqual(self.c.post('/api/reaction-methods',json=bad).status_code,422)
        bad=self.method();bad['stages'][0]['temp_min_c']=11
        self.assertEqual(self.c.post('/api/reaction-methods',json=bad).status_code,422)
    def test_02_batch_snapshot_sequence_qc(self):
        m=self.save();ident=m['id'];url='/api/reaction-methods/'+ident
        preview=self.c.post(url+'/preview',json=m);self.assertEqual(preview.status_code,200,preview.text);self.assertEqual(preview.json()['total_feed_g'],68)
        res=self.c.post(url+'/batches',json=m);self.assertEqual(res.status_code,200,res.text);lot=res.json()['id'];base='/api/lots/'+lot
        m['stages'][0]['name']='改名新步骤';self.c.post('/api/reaction-methods',json=m)
        p=self.c.get(base+'/method-process').json();self.assertEqual(p['method']['version'],1);self.assertEqual(p['stages'][0]['plan']['name'],'聚合')
        self.assertEqual(self.c.post(base+'/transition',json={'status':'QC_PENDING'}).status_code,422)
        with self.assertRaises(sqlite3.IntegrityError):
            with server.connect(True) as c:c.execute("UPDATE lots SET status='QC_PENDING' WHERE id=?",(lot,))
        record=dict(temp_min_c=-2,temp_max_c=8,hold_hours=2,confirmed=True,note='实测记录')
        self.assertEqual(self.c.post(base+'/method-stages/2/complete',json=record).status_code,422)
        self.assertEqual(self.c.post(base+'/method-stages/1/complete',json=dict(record,hold_hours=1)).status_code,422)
        self.assertEqual(self.c.post(base+'/method-stages/1/complete',json=dict(record,temp_min_c=-6)).status_code,422)
        self.assertEqual(self.c.post(base+'/method-stages/1/complete',json=record).status_code,200)
        self.assertEqual(self.c.post(base+'/method-stages/1/complete',json=record).status_code,409)
        with self.assertRaises(sqlite3.IntegrityError):
            with server.connect(True) as c:c.execute("UPDATE method_batch_stages SET status='PENDING' WHERE lot_id=? AND number=1",(lot,))
        r=self.c.post(base+'/method-stages/2/complete',json=dict(record,temp_min_c=45,temp_max_c=55,hold_hours=24));self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.c.get(base+'/method-process').json()['status'],'QC_PENDING')
        qc=dict(status='RELEASED',sc=99.5,mn=10000,mw=20000,viscosity=300,water_ppm=100,stock=12,form='DRY_POWDER',solvent='SV-01')
        self.assertEqual(self.c.post(base+'/transition',json=qc).status_code,200)
        lab=TestClient(server.app);lab.headers['X-Pipbo']='1';lab.post('/api/login',json={'name':'lab','password':'lab-test-1234'})
        self.assertEqual(lab.get(base+'/method-process').status_code,403)
        public=next(x for x in lab.get('/api/lots').json() if x['id']==lot)
        self.assertNotIn('method_plan',public);self.assertNotIn('method_snapshot',public)
