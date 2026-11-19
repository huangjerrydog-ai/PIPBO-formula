import csv
import io
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from urllib.parse import quote
import test_workflow as fixtures
server=fixtures.server


class BatchImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(server.app);cls.client.headers['X-Pipbo']='1'
        if cls.client.get('/api/status').json()['setup']:
            cls.client.post('/api/setup',json={'name':'director','password':'testpass-1234'})
        assert cls.client.post('/api/login',json={'name':'director','password':'testpass-1234'}).status_code==200
        for name,role in [('batch-lab','LAB'),('batch-other','DIRECTOR')]:
            cls.client.post('/api/users',json={'name':name,'password':'batch-tests-1234','role':role})
        cls.lab=TestClient(server.app);cls.lab.headers['X-Pipbo']='1'
        cls.lab.post('/api/login',json={'name':'batch-lab','password':'batch-tests-1234'})
        cls.other=TestClient(server.app);cls.other.headers['X-Pipbo']='1'
        cls.other.post('/api/login',json={'name':'batch-other','password':'batch-tests-1234'})
        helper=fixtures.Workflow();helper.client=cls.client
        lot=helper.make_lot();data=helper.formula(lot);data['request_key']='batch-import-formula-001'
        r=cls.client.post('/api/formulations',json=data);assert r.status_code==200,r.text
        cls.formula_id=r.json()['id']

    def csv_file(self,headers,rows,encoding='utf-8-sig'):
        stream=io.StringIO(newline='');w=csv.writer(stream);w.writerow(headers);w.writerows(rows)
        return stream.getvalue().encode(encoding)

    def preview(self,kind,body,filename='data.csv',client=None):
        return (client or self.client).post('/api/imports/'+kind+'/preview',content=body,headers={'X-File-Name':quote(filename),'Content-Type':'application/octet-stream'})

    def commit(self,kind,job,client=None):
        return (client or self.client).post('/api/imports/'+kind+'/commit',json={'job_id':job})

    def measurement_file(self,rows):
        return self.csv_file(['外部记录ID','配方批号','膜厚(μm)','残膜率(%)','测试备注'],[[key,self.formula_id,film,95,note] for key,film,note in rows])

    def count(self,table):
        with server.connect() as c:return c.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_01_templates(self):
        for kind in ('materials','measurements'):
            r=self.client.get('/api/imports/'+kind+'/template?format=xlsx');self.assertEqual(r.status_code,200)
            book=load_workbook(io.BytesIO(r.content));self.assertEqual(book.sheetnames,['数据','填写说明'])
            self.assertEqual(book['数据'].freeze_panes,'A2');self.assertTrue(book['数据']['A1'].font.bold)
            self.assertGreater(book['填写说明'].max_row,5);book.close()
            r=self.client.get('/api/imports/'+kind+'/template?format=csv');self.assertTrue(r.content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(self.lab.get('/api/imports/materials/template').status_code,403)
        self.assertEqual(self.lab.get('/api/imports/measurements/template').status_code,200)
        self.assertEqual(TestClient(server.app).get('/api/imports/materials/template').status_code,401)

    def test_02_xlsx_materials_encryption_replay_and_gbk(self):
        book=Workbook();sheet=book.active
        sheet.append(['内部编码','物料名称','类别','适用路线','分子量(g/mol)','CAS','供应商','商品牌号'])
        sheet.append(['IMP-RAW-01','测试专用原料甲','SV','BOTH',123.45,'001-02-3','测试供应商','Grade-A'])
        sheet.append(['IMP-RAW-02','测试专用原料乙','DM','N',200.24,None,None,None])
        stream=io.BytesIO();book.save(stream);book.close();before=self.count('materials')
        r=self.preview('materials',stream.getvalue(),'原料.xlsx');self.assertEqual(r.status_code,200,r.text)
        report=r.json();self.assertEqual(report['summary'],{'total':2,'ready':2,'skipped':0,'errors':0})
        self.assertEqual(self.count('materials'),before)
        with server.connect() as c:
            stored=c.execute('SELECT payload FROM import_jobs WHERE id=?',(report['job_id'],)).fetchone()[0]
            self.assertNotIn('测试专用'.encode(),stored)
        result=self.commit('materials',report['job_id']);self.assertEqual(result.json()['imported'],2)
        self.assertEqual(self.commit('materials',report['job_id']).json(),result.json());self.assertEqual(self.count('materials'),before+2)
        with server.connect() as c:
            raw=c.execute("SELECT secret FROM materials WHERE code='IMP-RAW-01'").fetchone()[0]
            self.assertNotIn('测试供应商'.encode(),raw);self.assertEqual(server.decrypt(raw)['supplier'],'测试供应商')
        again=self.preview('materials',stream.getvalue(),'data.xlsx').json()
        self.assertEqual(again['summary']['skipped'],2);self.assertIsNone(again['job_id'])
        gbk=self.csv_file(['code','name','category','route','mw'],[['IMP-GBK','中文原料','SV','BOTH',99]],'gb18030')
        r=self.preview('materials',gbk);self.assertEqual(r.json()['rows'][0]['data']['name'],'中文原料')
        self.assertEqual(self.commit('materials',r.json()['job_id']).json()['imported'],1)

    def test_03_row_errors_headers_and_changed_existing_code(self):
        headers=['code','name','category','route','mw']
        body=self.csv_file(headers,[['IMP-BAD-01','a','DM','P',200],['IMP-BAD-02','b','SV','BOTH',-1],['IMP-DUP','c','SV','BOTH',1],['IMP-DUP','d','SV','BOTH',1]])
        before=self.count('materials');r=self.preview('materials',body);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['summary']['errors'],3);self.assertIsNone(r.json()['job_id']);self.assertEqual(self.count('materials'),before)
        missing=self.csv_file(['code','name'],[['x','a']]);self.assertEqual(self.preview('materials',missing).status_code,422)
        duplicate=self.csv_file(['code','内部编码','name','category','route','mw'],[['X','X','a','SV','BOTH',100]])
        self.assertEqual(self.preview('materials',duplicate).status_code,422)
        body=self.csv_file(headers,[['IMP-STALE','Old upload','SV','BOTH',1],['IMP-STALE-2','Other','SV','BOTH',2]])
        job=self.preview('materials',body).json()['job_id']
        r=self.client.post('/api/materials',json=dict(code='IMP-STALE',name='Created later',category='SV',route='BOTH',mw=3));self.assertEqual(r.status_code,200)
        r=self.commit('materials',job);self.assertEqual(r.json(),{'imported':1,'skipped':1,'total':2})
        with server.connect() as c:
            m=c.execute("SELECT secret FROM materials WHERE code='IMP-STALE'").fetchone()
            self.assertEqual(server.decrypt(m['secret'])['name'],'Created later')

    def test_04_test_data_zero_duplicates_and_reference(self):
        body=self.measurement_file([('EXT-TEST-001',0,'zero is measured'),('EXT-TEST-002',8.5,'仪器A')]);before=self.count('measurements')
        r=self.preview('measurements',body);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.count('measurements'),before)
        result=self.commit('measurements',r.json()['job_id']);self.assertEqual(result.json()['imported'],2)
        repeat=self.preview('measurements',body).json();self.assertEqual(repeat['summary']['skipped'],2)
        changed=self.preview('measurements',self.measurement_file([('EXT-TEST-001',4,'changed')])).json()
        self.assertEqual(changed['summary']['errors'],1);self.assertIsNone(changed['job_id'])
        invalid=self.csv_file(['external_id','formula_id','film_um'],[['EXT-TEST-BAD','NO-SUCH-FORMULA',8]])
        r=self.preview('measurements',invalid);self.assertEqual(r.json()['summary']['errors'],1)
        self.assertEqual(self.count('measurements'),before+2)
        duplicate=self.preview('measurements',self.measurement_file([('DUP-IN-FILE',1,''),('DUP-IN-FILE',1,'')])).json()
        self.assertEqual(duplicate['summary']['errors'],1)
        with server.connect() as c:
            m=c.execute("SELECT m.payload FROM measurements m JOIN imported_test_keys i ON i.measurement_id=m.id WHERE external_id='EXT-TEST-001'").fetchone()
            self.assertEqual(server.json.loads(m['payload'])['film_um'],0)

    def test_05_permissions_ownership_concurrent_commit(self):
        body=self.measurement_file([('EXT-CONCURRENT',12,'private-note')]);r=self.preview('measurements',body,client=self.lab)
        self.assertEqual(r.status_code,200,r.text);job=r.json()['job_id']
        self.assertEqual(self.commit('measurements',job,client=self.other).status_code,404)
        self.assertEqual(self.preview('materials',b'bad',client=self.lab).status_code,403)
        self.assertEqual(self.commit('materials',job,client=self.lab).status_code,403)
        before=self.count('measurements')
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda _:self.commit('measurements',job,client=self.lab),range(2)))
        self.assertTrue(all(r.status_code==200 for r in results));self.assertEqual(self.count('measurements'),before+1)
        self.assertEqual(results[0].json(),results[1].json())
        self.assertTrue(all('note' not in r['values'] for r in self.lab.get('/api/measurements').json()))
        # Revalidation rejects a conflicting record added after the original preview.
        original=self.measurement_file([('EXT-RACE-CONFLICT',1,''),('EXT-RACE-UNTOUCHED',3,'')])
        pending=self.preview('measurements',original).json()['job_id']
        other=self.preview('measurements',self.measurement_file([('EXT-RACE-CONFLICT',2,'')])).json()['job_id']
        self.assertEqual(self.commit('measurements',other).status_code,200)
        before=self.count('measurements');r=self.commit('measurements',pending)
        self.assertEqual(r.status_code,409);self.assertEqual(self.count('measurements'),before)

    def test_06_late_failure_rolls_back_every_row(self):
        body=self.measurement_file([('EXT-ROLLBACK-FIRST',1,''),('EXT-ROLLBACK-FAIL',2,'')])
        job=self.preview('measurements',body).json()['job_id'];before=self.count('measurements')
        with server.connect(True) as c:
            c.execute("CREATE TRIGGER import_test_failure BEFORE INSERT ON imported_test_keys WHEN NEW.external_id='EXT-ROLLBACK-FAIL' BEGIN SELECT RAISE(ABORT,'simulated disk/write failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError): self.commit('measurements',job)
            self.assertEqual(self.count('measurements'),before)
            with server.connect() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM imported_test_keys WHERE external_id='EXT-ROLLBACK-FIRST'").fetchone()[0],0)
                self.assertEqual(c.execute('SELECT status FROM import_jobs WHERE id=?',(job,)).fetchone()[0],'PENDING')
        finally:
            with server.connect(True) as c:c.execute('DROP TRIGGER import_test_failure')
        self.assertEqual(self.commit('measurements',job).json()['imported'],2)

    def test_07_limits_formulas_percentage_expiry(self):
        self.assertEqual(self.preview('materials',b'x'*(5*1024*1024+1)).status_code,413)
        too_many=self.csv_file(['code','name','category','route','mw'],[[f'LIMIT-{n}','a','SV','BOTH',100] for n in range(1001)])
        self.assertEqual(self.preview('materials',too_many).status_code,422)
        self.assertEqual(self.preview('materials',b'not-an-excel','old.xls').status_code,422)
        self.assertEqual(self.preview('materials',b'corrupted','file.xlsx').status_code,422)
        book=Workbook();sheet=book.active;sheet.append(['external_id','formula_id','film_um','residual_pct'])
        sheet.append(['EXT-FORMULA',self.formula_id,'=1+1',95]);sheet.append(['EXT-PERCENT',self.formula_id,8,.95]);sheet['D3'].number_format='0%'
        stream=io.BytesIO();book.save(stream);book.close()
        r=self.preview('measurements',stream.getvalue(),'formula.xlsx');self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['summary']['errors'],2)
        job=self.preview('measurements',self.measurement_file([('EXT-EXPIRED',1,'')])).json()['job_id']
        with server.connect(True) as c:c.execute('UPDATE import_jobs SET expires=0 WHERE id=?',(job,))
        self.assertEqual(self.commit('measurements',job).status_code,409)
        # Blank spreadsheet rows do not consume the 1000-record quota.
        blanks=self.csv_file(['external_id','formula_id','film_um'],[['','','']]*1001+[['EXT-BLANK-PADDING',self.formula_id,1]])
        report=self.preview('measurements',blanks).json();self.assertEqual(report['summary']['ready'],1)
        # An empty row is ignored; an actual zero metric is retained.
        content=self.csv_file(['external_id','formula_id','film_um'],[['','',''],['EXT-ZERO',self.formula_id,0]])
        r=self.preview('measurements',content).json();self.assertEqual(r['summary']['total'],1);self.assertEqual(r['summary']['ready'],1)

if __name__=='__main__': unittest.main()
