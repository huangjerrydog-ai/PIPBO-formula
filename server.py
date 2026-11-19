"""Local PSPI/PBO laboratory application. SQLite transactions, encrypted identities."""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import unquote
from import_tools import MAX_BYTES, SCHEMAS, parse_file, template, validate_material, validate_measurement, text
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from catalog import seed_rows
from copolymer import solve_molar
from process_control import validate_stage
from engine import RuleError, formulation, material, num, synthesis, balance_feed

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('PIPBO_DATA',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
KEY=DATA/'vault.key'
if not KEY.exists():
    if (DATA/'pipbo.sqlite3').exists():
        raise RuntimeError('vault.key缺失，请从备份恢复原密钥；禁止生成新密钥覆盖既有数据')
    fd=os.open(KEY,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'wb') as f: f.write(AESGCM.generate_key(bit_length=256))
CIPHER=AESGCM(KEY.read_bytes())
DB=DATA/'pipbo.sqlite3'
COOKIE='pipbo_session_'+hashlib.sha256(str(DATA.resolve()).encode()).hexdigest()[:12]

def now(): return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
def pack(value): return json.dumps(value,ensure_ascii=False,allow_nan=False)
def encrypt(value):
    nonce=secrets.token_bytes(12)
    return nonce+CIPHER.encrypt(nonce,pack(value).encode(),b'pipbo-v1')
def decrypt(value): return json.loads(CIPHER.decrypt(value[:12],value[12:],b'pipbo-v1'))

@contextmanager
def connect(write=False):
    c=sqlite3.connect(DB,timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        if write: c.execute('BEGIN IMMEDIATE')
        yield c
        if write: c.commit()
    except BaseException:
        c.rollback(); raise
    finally: c.close()

with connect(True) as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS materials(code TEXT PRIMARY KEY,category TEXT NOT NULL,route TEXT NOT NULL,mw REAL NOT NULL,secret BLOB NOT NULL,source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,password TEXT NOT NULL,role TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,name TEXT NOT NULL,expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS lots(id TEXT PRIMARY KEY,route TEXT NOT NULL,status TEXT NOT NULL,stock REAL NOT NULL DEFAULT 0,payload TEXT NOT NULL,qc TEXT,solvent TEXT,expiry TEXT,created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS formulas(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL,created TEXT NOT NULL,author TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY,formula_id TEXT NOT NULL REFERENCES formulas(id),payload TEXT NOT NULL,created TEXT NOT NULL,author TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,created TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,object_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,lot_id TEXT NOT NULL REFERENCES lots(id),delta REAL NOT NULL,formula_id TEXT,created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS counters(prefix TEXT PRIMARY KEY,seq INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS import_jobs(id TEXT PRIMARY KEY,actor TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL,status TEXT NOT NULL,expires REAL NOT NULL,created TEXT NOT NULL,result TEXT);
    CREATE TABLE IF NOT EXISTS reaction_methods(id TEXT PRIMARY KEY,version INTEGER NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),payload TEXT NOT NULL,updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reaction_method_versions(method_id TEXT NOT NULL REFERENCES reaction_methods(id),version INTEGER NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL,actor TEXT NOT NULL,PRIMARY KEY(method_id,version));
    CREATE TABLE IF NOT EXISTS method_batch_stages(lot_id TEXT NOT NULL REFERENCES lots(id),number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 12),status TEXT NOT NULL CHECK(status IN('PENDING','COMPLETED')),plan TEXT NOT NULL,record TEXT,completed TEXT,actor TEXT,PRIMARY KEY(lot_id,number));
    CREATE TRIGGER IF NOT EXISTS gate_method_before_qc BEFORE UPDATE OF status ON lots WHEN NEW.status IN('QC_PENDING','RELEASED') AND EXISTS(SELECT 1 FROM method_batch_stages WHERE lot_id=NEW.id AND status!='COMPLETED') BEGIN SELECT RAISE(ABORT,'Method steps must complete before QC/release'); END;
    CREATE TRIGGER IF NOT EXISTS freeze_method_stage BEFORE UPDATE ON method_batch_stages WHEN OLD.status='COMPLETED' BEGIN SELECT RAISE(ABORT,'Completed method stages are immutable'); END;
    CREATE TABLE IF NOT EXISTS synthesis_molar_items(id INTEGER PRIMARY KEY,lot_id TEXT NOT NULL REFERENCES lots(id),code TEXT NOT NULL REFERENCES materials(code),group_name TEXT NOT NULL,mol_pct REAL NOT NULL,moles REAL,mass REAL NOT NULL CHECK(mass>=0),purity REAL NOT NULL,stage INTEGER NOT NULL,volume_ml REAL);
    CREATE TABLE IF NOT EXISTS process_stages(lot_id TEXT NOT NULL REFERENCES lots(id),stage_no INTEGER NOT NULL CHECK(stage_no BETWEEN 1 AND 4),name TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED')),template TEXT NOT NULL,started_at TEXT,completed_at TEXT,start_record TEXT,completion_record TEXT,actor TEXT,PRIMARY KEY(lot_id,stage_no));
    CREATE TABLE IF NOT EXISTS workup_logs(lot_id TEXT PRIMARY KEY REFERENCES lots(id),wash_cycles_count INTEGER NOT NULL CHECK(wash_cycles_count>=1),dry_temp_min_c REAL NOT NULL,dry_temp_max_c REAL NOT NULL,dry_hours REAL NOT NULL CHECK(dry_hours>=24),theoretical_g REAL NOT NULL CHECK(theoretical_g>0),actual_g REAL NOT NULL CHECK(actual_g>=0),yield_pct REAL NOT NULL,record TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS gate_process_before_qc BEFORE UPDATE OF status ON lots WHEN NEW.status IN ('QC_PENDING','RELEASED') AND EXISTS(SELECT 1 FROM process_stages WHERE lot_id=NEW.id) AND ((SELECT count(*) FROM process_stages WHERE lot_id=NEW.id AND status='COMPLETED')!=4 OR NOT EXISTS(SELECT 1 FROM workup_logs WHERE lot_id=NEW.id)) BEGIN SELECT RAISE(ABORT,'Four process stages and workup must complete before QC/release'); END;
    CREATE TRIGGER IF NOT EXISTS freeze_completed_stage BEFORE UPDATE ON process_stages WHEN OLD.status='COMPLETED' BEGIN SELECT RAISE(ABORT,'Completed process stages are immutable'); END;
    CREATE TABLE IF NOT EXISTS imported_test_keys(external_id TEXT PRIMARY KEY,digest TEXT NOT NULL,measurement_id INTEGER UNIQUE NOT NULL REFERENCES measurements(id));
    ''')
    for m in seed_rows():
        c.execute('INSERT OR IGNORE INTO materials VALUES(?,?,?,?,?,?)',(m['code'],m['category'],m['route'],m['mw'],encrypt(m['identity']),m['source']))
        existing=c.execute('SELECT secret FROM materials WHERE code=?',(m['code'],)).fetchone()
        identity=decrypt(existing['secret'])
        if not identity.get('cn_name') and identity.get('cas')==m['identity']['cas']:
            identity['cn_name']=m['identity']['cn_name']
            c.execute('UPDATE materials SET secret=? WHERE code=?',(encrypt(identity),m['code']))
    # Older custom materials may already have a Chinese name in the name field.
    for record in c.execute('SELECT code,secret FROM materials').fetchall():
        identity=decrypt(record['secret']);name=str(identity.get('name',''))
        if not identity.get('cn_name') and any('\u4e00'<=char<='\u9fff' for char in name):
            identity['cn_name']=name
            c.execute('UPDATE materials SET secret=? WHERE code=?',(encrypt(identity),record['code']))
os.chmod(DB,0o600)

def catalog(c, identities=False):
    result={}
    for row in c.execute('SELECT * FROM materials ORDER BY code'):
        m=dict(row); secret=m.pop('secret')
        if identities: m.update(decrypt(secret))
        result[m['code']]=m
    return result

def lot_data(row, full=True):
    m=dict(row); raw=json.loads(m.pop('payload')); qc=json.loads(m['qc']) if m['qc'] else {}
    m['qc']=qc
    if full: m.update(raw)
    m['process_required']=bool(raw.get('process_required'))
    m['method_required']=bool(raw.get('method_required'))
    m['expired']=bool(m.get('expiry') and m['expiry']<now()[:10])
    return m

def audit(c,user,action,obj):
    c.execute('INSERT INTO audit(created,actor,action,object_id) VALUES(?,?,?,?)',(now(),user,action,obj))

def serial(c,kind,route):
    prefix=f'{kind}-'+('PSPI-N' if route=='N' else 'PBO-P')+'-'+now()[:10].replace('-','')
    c.execute('INSERT INTO counters VALUES(?,1) ON CONFLICT(prefix) DO UPDATE SET seq=seq+1',(prefix,))
    n=c.execute('SELECT seq FROM counters WHERE prefix=?',(prefix,)).fetchone()[0]
    return f'{prefix}-{n:03}'

def password_hash(password,salt=None):
    if not isinstance(password,str) or len(password)<10 or len(password)>256: raise RuleError('密码长度需为10–256个字符')
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()

def username(name):
    if not isinstance(name,str) or not name.strip() or len(name)>40: raise RuleError('用户名需为1–40个字符')
    return name.strip()

app=FastAPI(title='PIPBO · 光敏材料研发',docs_url=None,redoc_url=None,openapi_url=None)

@app.exception_handler(RuleError)
async def rule_error(request,exc): return JSONResponse({'detail':str(exc)},status_code=422)

@app.middleware('http')
async def protection(request,call_next):
    # Reject DNS rebinding and cross-origin mutation; only loopback is served.
    host=request.headers.get('host','').split(':')[0]
    if host not in ('localhost','127.0.0.1','testserver'): return JSONResponse({'detail':'仅允许本机访问'},status_code=403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        if request.headers.get('x-pipbo')!='1' or (origin and origin!=str(request.base_url).rstrip('/')):
            return JSONResponse({'detail':'请求来源校验失败'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

def user(request:Request):
    token=request.cookies.get(COOKIE,'')
    with connect() as c:
        row=c.execute('SELECT u.name,u.role FROM sessions s JOIN users u ON u.name=s.name WHERE s.token=? AND s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row: raise HTTPException(401,'请先登录')
    return dict(row)

def director(u=Depends(user)):
    if u['role']!='DIRECTOR': raise HTTPException(403,'此操作需要研发管理员权限')
    return u

@app.get('/api/status')
def status(request:Request):
    with connect() as c: setup=c.execute('SELECT count(*) FROM users').fetchone()[0]==0
    try: u=user(request)
    except HTTPException: u=None
    return dict(setup=setup,user=u,version='0.5.0')

@app.post('/api/setup')
def setup(d:dict):
    name=username(d.get('name')); pw=password_hash(d.get('password'))
    with connect(True) as c:
        if c.execute('SELECT count(*) FROM users').fetchone()[0]: raise HTTPException(409,'已完成初始化，请登录')
        c.execute('INSERT INTO users VALUES(?,?,?)',(name,pw,'DIRECTOR')); audit(c,name,'INITIAL_SETUP',name)
    return {'ok':True}

FAILURES={}
@app.post('/api/login')
def login(d:dict,request:Request):
    ip=request.client.host if request.client else 'local'; t=time.time()
    previous=[x for x in FAILURES.get(ip,[]) if t-x<300]; FAILURES[ip]=previous
    if len(previous)>=8: raise HTTPException(429,'登录失败次数过多，请5分钟后再试')
    with connect(True) as c:
        row=c.execute('SELECT * FROM users WHERE name=?',(str(d.get('name','')),)).fetchone()
        try: match=row and hmac.compare_digest(row['password'],password_hash(d.get('password'),row['password'].split(':')[0]))
        except RuleError: match=False
        if not match:
            previous.append(t); raise HTTPException(401,'用户名或密码错误')
        token=secrets.token_urlsafe(40)
        c.execute('DELETE FROM sessions WHERE expires<?',(t,))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['name'],t+8*3600))
        audit(c,row['name'],'LOGIN',row['name'])
    FAILURES.pop(ip,None)
    response=JSONResponse({'ok':True}); response.set_cookie(COOKIE,token,httponly=True,samesite='strict',max_age=8*3600)
    return response

@app.post('/api/logout')
def logout(request:Request,u=Depends(user)):
    token=request.cookies.get(COOKIE,'')
    with connect(True) as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response=JSONResponse({'ok':True}); response.delete_cookie(COOKIE); return response

@app.post('/api/users')
def add_user(d:dict,u=Depends(director)):
    name=username(d.get('name')); pw=password_hash(d.get('password'))
    role=d.get('role','LAB')
    if role not in ('DIRECTOR','LAB'): raise RuleError('角色不合法')
    with connect(True) as c:
        if c.execute('SELECT 1 FROM users WHERE name=?',(name,)).fetchone(): raise HTTPException(409,'用户名已存在')
        c.execute('INSERT INTO users VALUES(?,?,?)',(name,pw,role)); audit(c,u['name'],'CREATE_USER',name)
    return {'ok':True}

@app.get('/api/materials')
def materials(u=Depends(user)):
    with connect(True) as c:
        result=catalog(c,u['role']=='DIRECTOR')
        if u['role']=='DIRECTOR': audit(c,u['name'],'VIEW_MATERIAL_IDENTITIES','catalog')
    return list(result.values())

@app.post('/api/materials')
def add_material(d:dict,u=Depends(director)):
    data=validate_material(d)
    with connect(True) as c:
        if c.execute('SELECT 1 FROM materials WHERE code=?',(data['code'],)).fetchone(): raise HTTPException(409,'编码已存在，物料不可覆盖')
        c.execute('INSERT INTO materials VALUES(?,?,?,?,?,?)',(data['code'],data['category'],data['route'],data['mw'],encrypt(data['identity']),'用户录入，待核对'))
        audit(c,u['name'],'ADD_MATERIAL',data['code'])
    return {'ok':True}

@app.post('/api/materials/{code}/names')
def edit_material_names(code:str,d:dict,u=Depends(director)):
    cn_name=text(d.get('cn_name'),'中文名称',1000)
    if not cn_name: raise RuleError('中文名称必填')
    with connect(True) as c:
        row=c.execute('SELECT secret FROM materials WHERE code=?',(code,)).fetchone()
        if not row: raise HTTPException(404,'物料不存在')
        identity=decrypt(row['secret']);identity['cn_name']=cn_name
        if 'name' in d:
            name=text(d['name'],'英文名称/简称',1000)
            if not name: raise RuleError('英文名称/简称不能为空')
            identity['name']=name
        c.execute('UPDATE materials SET secret=? WHERE code=?',(encrypt(identity),code))
        audit(c,u['name'],'UPDATE_MATERIAL_NAMES',code)
    return {'ok':True}


@app.post('/api/synthesis/balance')
def balance_synthesis(d:dict,u=Depends(director)):
    with connect() as c: return balance_feed(d,catalog(c))


@app.post('/api/synthesis/preview')
def preview_synthesis(d:dict,u=Depends(director)):
    with connect() as c: return synthesis(d,catalog(c))

@app.post('/api/lots')
def create_lot(d:dict,u=Depends(director)):
    with connect(True) as c:
        estimate=synthesis(d,catalog(c)); ident=serial(c,'SYN',d['route'])
        payload={k:d.get(k) for k in ('monomers', 'conversion', 'synthesis_path', 'stoichiometry_mode', 'excess_pct', 'graft_code', 'graft_degree', 'sidechain_excess_pct', 'sidechain_purity', 'sidechain_mass', 'acyl_eq', 'acyl_purity', 'acyl_mass', 'step1_note', 'step2_note', 'step3_note', 'note')}; payload['estimate']=estimate
        c.execute('INSERT INTO lots(id,route,status,payload,created) VALUES(?,?,?,?,?)',(ident,d['route'],'SYNTHESIZING',pack(payload),now()))
        audit(c,u['name'],'CREATE_SYNTHESIS',ident)
    return {'id':ident}

@app.get('/api/lots')
def lots(u=Depends(user)):
    with connect() as c: return [lot_data(r,u['role']=='DIRECTOR') for r in c.execute('SELECT * FROM lots ORDER BY created DESC,id DESC')]

@app.post('/api/lots/{ident}/transition')
def transition(ident:str,d:dict,u=Depends(director)):
    target=d.get('status')
    with connect(True) as c:
        row=c.execute('SELECT * FROM lots WHERE id=?',(ident,)).fetchone()
        if not row: raise HTTPException(404,'批次不存在')
        current=row['status']
        allowed={'SYNTHESIZING':{'QC_PENDING','QUARANTINED'},'QC_PENDING':{'RELEASED','QUARANTINED'},'RELEASED':{'QUARANTINED'}}
        if target not in allowed.get(current,set()): raise RuleError('状态转换不允许')
        if target in ('QC_PENDING','RELEASED') and json.loads(row['payload']).get('method_required'):
            if c.execute("SELECT count(*) FROM method_batch_stages WHERE lot_id=? AND status!='COMPLETED'",(ident,)).fetchone()[0]:raise RuleError('方法步骤尚未全部完成，不能提交质检或放行')
        if target in ('QC_PENDING','RELEASED') and json.loads(row['payload']).get('process_required'):
            completed=c.execute("SELECT count(*) FROM process_stages WHERE lot_id=? AND status='COMPLETED'",(ident,)).fetchone()[0]
            workup=c.execute('SELECT * FROM workup_logs WHERE lot_id=?',(ident,)).fetchone()
            if completed!=4 or not workup:raise RuleError('四阶段与纯化记录未完成，不能提交质检或放行')
            if target=='RELEASED':
                if d.get('form')!='DRY_POWDER':raise RuleError('v1.1 PAE四阶段模板只允许干粉出料')
                if num(d.get('stock'),'入库质量')>workup['actual_g']+1e-7:raise RuleError('入库质量不能超过纯化记录中的实际干粉产量')
        if target=='RELEASED':
            qc={k:num(d.get(k),k,hi=100 if k=='sc' else None,zero=(k=='water_ppm')) for k in ('sc','mn','mw','viscosity','water_ppm')}
            if qc['mw']<qc['mn']: raise RuleError('Mw不可小于Mn')
            qc['pdi']=qc['mw']/qc['mn']; qc['form']=d.get('form')
            if qc['form'] not in ('MOTHER_LIQUOR','DRY_POWDER'): raise RuleError('请选择出料形态')
            stock=num(d.get('stock'),'入库质量')
            solvent=d.get('solvent')
            if qc['sc']<100:
                profile=d.get('solvent_profile') or [{'code':solvent,'ratio':100}]
                if not isinstance(profile,list) or not profile: raise RuleError('请登记携带溶剂组成')
                seen=set(); checked=[]
                for part in profile:
                    code=part.get('code'); material(catalog(c),code,['SV'])
                    if code in seen: raise RuleError('携带溶剂不可重复')
                    seen.add(code); checked.append({'code':code,'ratio':num(part.get('ratio'),'携带溶剂占比%',hi=100,zero=True)})
                if abs(sum(x['ratio'] for x in checked)-100)>1e-6: raise RuleError('携带溶剂占比必须合计100%')
                qc['solvent_profile']=checked
                solvent=checked[0]['code']
            expiry=d.get('expiry') or None
            if expiry:
                try: expiry=datetime.strptime(expiry,'%Y-%m-%d').date().isoformat()
                except (ValueError,TypeError): raise RuleError('有效期格式应为YYYY-MM-DD') from None
                if expiry<now()[:10]: raise RuleError('入库有效期不得早于今天')
            c.execute('UPDATE lots SET status=?,stock=?,qc=?,solvent=?,expiry=? WHERE id=?',(target,stock,pack(qc),solvent,expiry,ident))
            c.execute('INSERT INTO ledger(lot_id,delta,created) VALUES(?,?,?)',(ident,stock,now()))
        else: c.execute('UPDATE lots SET status=? WHERE id=?',(target,ident))
        audit(c,u['name'],target,ident)
    return {'ok':True}

@app.post('/api/formulations/preview')
def preview_formula(d:dict,u=Depends(director)):
    with connect() as c:
        all_lots={r['id']:lot_data(r) for r in c.execute('SELECT * FROM lots')}
        return formulation(d,all_lots,catalog(c))

@app.post('/api/formulations')
def create_formula(d:dict,u=Depends(director)):
    request_key=d.get('request_key')
    if not isinstance(request_key,str) or len(request_key)<16 or len(request_key)>100: raise RuleError('缺少有效的请求唯一标识')
    digest=hashlib.sha256(pack({k:v for k,v in d.items() if k!='request_key'}).encode()).hexdigest()
    with connect(True) as c:
        previous=c.execute('SELECT id,digest FROM formulas WHERE request_key=?',(request_key,)).fetchone()
        if previous:
            if previous['digest']!=digest: raise HTTPException(409,'重复请求的内容不一致')
            return {'id':previous['id']}
        all_lots={r['id']:lot_data(r) for r in c.execute('SELECT * FROM lots')}
        result=formulation(d,all_lots,catalog(c)); ident=serial(c,'FRM',d['route'])
        for lot in sorted(result['lots'],key=lambda x:x['id']):
            cur=c.execute("UPDATE lots SET stock=MAX(0,stock-?),status=CASE WHEN stock-?<=1e-8 THEN 'DEPLETED' ELSE status END WHERE id=? AND status='RELEASED' AND stock+1e-8>=?",(lot['mass'],lot['mass'],lot['id'],lot['mass']))
            if cur.rowcount!=1: raise HTTPException(409,'批次库存或状态已改变，请刷新')
            c.execute('INSERT INTO ledger(lot_id,delta,formula_id,created) VALUES(?,?,?,?)',(lot['id'],-lot['mass'],ident,now()))
        c.execute('INSERT INTO formulas VALUES(?,?,?,?,?,?,?)',(ident,request_key,digest,pack(d),pack(result),now(),u['name']))
        audit(c,u['name'],'CREATE_FORMULATION',ident)
    return {'id':ident}

@app.get('/api/formulations')
def formulas(u=Depends(user)):
    with connect() as c:
        result=[]
        for row in c.execute('SELECT * FROM formulas ORDER BY created DESC,id DESC'):
            m={k:row[k] for k in ('id','created','author')}; m['result']=json.loads(row['result'])
            if u['role']=='DIRECTOR': m['input']=json.loads(row['payload'])
            result.append(m)
        return result

@app.get('/api/formulations/{ident}/csv')
def weighing_csv(ident:str,u=Depends(user)):
    with connect() as c:
        row=c.execute('SELECT result FROM formulas WHERE id=?',(ident,)).fetchone()
        if not row: raise HTTPException(404,'配方不存在')
        r=json.loads(row['result'])
    f=io.StringIO(); writer=csv.writer(f); writer.writerow(['配方批号','内部编码','类别','实际称量(g)','干固体(g)','wt%'])
    for x in r['lines']: writer.writerow([ident,x['code'],x['kind'],f"{x['mass']:.6f}",f"{x['dry_g']:.6f}",f"{x['wt_pct']:.6f}"])
    return Response('\ufeff'+f.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{ident}.csv"'})

@app.post('/api/measurements')
def measure(d:dict,u=Depends(user)):
    data=validate_measurement(d);ident=data['formula_id'];result=data['values']
    with connect(True) as c:
        if not c.execute('SELECT 1 FROM formulas WHERE id=?',(ident,)).fetchone(): raise HTTPException(404,'配方不存在')
        c.execute('INSERT INTO measurements(formula_id,payload,created,author) VALUES(?,?,?,?)',(ident,pack(result),now(),u['name']))
        audit(c,u['name'],'ADD_MEASUREMENT',ident)
    return {'ok':True}

@app.get('/api/measurements')
def measurements(u=Depends(user)):
    with connect() as c:
        results=[]
        for row in c.execute('SELECT m.*,i.external_id FROM measurements m LEFT JOIN imported_test_keys i ON i.measurement_id=m.id ORDER BY m.id DESC'):
            m=dict(row); m['values']=json.loads(m.pop('payload'))
            if u['role']!='DIRECTOR': m['values'].pop('note',None)
            results.append(m)
        return results

@app.get('/api/audit')
def audit_rows(u=Depends(director)):
    with connect() as c: return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 500')]

# Batch imports are staged encrypted, then revalidated and committed atomically.
def import_access(kind,u):
    if kind not in SCHEMAS: raise HTTPException(404,'导入类别不存在')
    if kind=='materials' and u['role']!='DIRECTOR': raise HTTPException(403,'原料批量导入需要研发管理员权限')


def check_import_rows(kind,rows,c):
    results=[];seen=set()
    for row in rows:
        number=row['row'];d=row['data'];key=str(d.get('code' if kind=='materials' else 'external_id') or '').strip()
        errors=list(row.get('parse_errors',[]));normalized=None;state='ready';message='待导入'
        try:
            normalized=validate_material(d) if kind=='materials' else validate_measurement(d,imported=True)
            key=normalized['code' if kind=='materials' else 'external_id']
            if key in seen: errors.append('文件内唯一标识重复，请合并或修改后重试')
            seen.add(key)
            if kind=='materials':
                if c.execute('SELECT 1 FROM materials WHERE code=?',(key,)).fetchone():
                    state='skip';message='内部编码已存在，跳过；本行所有字段均不覆盖现有物料'
            else:
                if not c.execute('SELECT 1 FROM formulas WHERE id=?',(normalized['formula_id'],)).fetchone(): errors.append('关联的配方批号不存在，请先在系统中建立配方')
                digest=hashlib.sha256(pack(normalized).encode()).hexdigest()
                prior=c.execute('SELECT digest FROM imported_test_keys WHERE external_id=?',(key,)).fetchone()
                if prior:
                    if hmac.compare_digest(prior['digest'],digest): state='skip';message='同一外部记录ID与内容已导入，跳过'
                    else: errors.append('外部记录ID已存在且内容不同；请核对记录，修改ID后新增')
        except RuleError as exc: errors.append(str(exc))
        if errors: state='error';message='；'.join(errors)
        display={k:('' if v is None else str(v)) for k,v in d.items()}
        results.append({'row':number,'key':key,'status':state,'message':message,'data':display,'normalized':normalized})
    return results


def import_summary(rows):
    return {'total':len(rows),'ready':sum(x['status']=='ready' for x in rows),'skipped':sum(x['status']=='skip' for x in rows),'errors':sum(x['status']=='error' for x in rows)}


@app.get('/api/imports/{kind}/template')
def import_template(kind:str,format:str='xlsx',u=Depends(user)):
    import_access(kind,u)
    content,mime=template(kind,format)
    return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="pipbo-{kind}-template.{format}"'})


@app.post('/api/imports/{kind}/preview')
async def import_preview(kind:str,request:Request,u=Depends(user)):
    import_access(kind,u)
    filename=unquote(request.headers.get('x-file-name',''))
    if not filename or len(filename)>255: raise RuleError('请提供有效的文件名')
    chunks=bytearray()
    async for chunk in request.stream():
        if len(chunks)+len(chunk)>MAX_BYTES: raise HTTPException(413,'文件不能超过5MB')
        chunks.extend(chunk)
    rows,warnings=parse_file(bytes(chunks),filename,kind)
    with connect(True) as c:
        checked=check_import_rows(kind,rows,c);summary=import_summary(checked);job=None
        c.execute("DELETE FROM import_jobs WHERE expires<? AND status='PENDING'",(time.time(),))
        if not summary['errors'] and summary['ready']:
            count=c.execute("SELECT count(*) FROM import_jobs WHERE actor=? AND status='PENDING'",(u['name'],)).fetchone()[0]
            if count>=20: raise RuleError('待确认预览过多，请完成已有导入或30分钟后重试')
            job=secrets.token_urlsafe(32)
            c.execute('INSERT INTO import_jobs(id,actor,kind,payload,status,expires,created) VALUES(?,?,?,?,?,?,?)',
                      (job,u['name'],kind,encrypt(rows),'PENDING',time.time()+1800,now()))
        audit(c,u['name'],'PREVIEW_'+kind.upper()+'_IMPORT',f"rows:{summary['total']};errors:{summary['errors']}")
    public=[{k:v for k,v in row.items() if k!='normalized'} for row in checked]
    return dict(job_id=job,summary=summary,rows=public,warnings=warnings,expires_in=1800 if job else None)


@app.post('/api/imports/{kind}/commit')
def import_commit(kind:str,d:dict,u=Depends(user)):
    import_access(kind,u)
    ident=d.get('job_id')
    if not isinstance(ident,str) or len(ident)>100: raise RuleError('请先预览文件')
    with connect(True) as c:
        job=c.execute('SELECT * FROM import_jobs WHERE id=?',(ident,)).fetchone()
        if not job or job['actor']!=u['name'] or job['kind']!=kind: raise HTTPException(404,'预览记录不存在或不属于当前账户')
        if job['status']=='COMMITTED': return json.loads(job['result'])
        if job['expires']<time.time(): raise HTTPException(409,'预览已过期，请重新上传文件预览')
        rows=decrypt(job['payload']);checked=check_import_rows(kind,rows,c);summary=import_summary(checked)
        if summary['errors']:
            public=[{k:v for k,v in row.items() if k!='normalized'} for row in checked]
            return JSONResponse({'detail':'数据已发生变化，整批未保存；请修正后重新预览','summary':summary,'rows':public},status_code=409)
        imported=0
        for row in checked:
            if row['status']!='ready': continue
            data=row['normalized']
            if kind=='materials':
                c.execute('INSERT INTO materials VALUES(?,?,?,?,?,?)',(data['code'],data['category'],data['route'],data['mw'],encrypt(data['identity']),'批量导入，待核对'))
            else:
                cur=c.execute('INSERT INTO measurements(formula_id,payload,created,author) VALUES(?,?,?,?)',
                              (data['formula_id'],pack(data['values']),now(),u['name']))
                digest=hashlib.sha256(pack(data).encode()).hexdigest()
                c.execute('INSERT INTO imported_test_keys VALUES(?,?,?)',(data['external_id'],digest,cur.lastrowid))
            imported+=1
        result={'imported':imported,'skipped':summary['skipped'],'total':summary['total']}
        c.execute("UPDATE import_jobs SET status='COMMITTED',result=? WHERE id=?",(pack(result),ident))
        audit(c,u['name'],'COMMIT_'+kind.upper()+'_IMPORT',f"added:{imported};skipped:{summary['skipped']}")
    return result


# v1.1 supplemental multi-monomer designs and immutable four-stage ELN records.
@app.post('/api/synthesis/molar/preview')
@app.post('/api/v1/synthesis/calculate-stoichiometry')
@app.post('/api/v1/synthesis/pae-3step/calculate')
def preview_molar(d:dict,u=Depends(director)):
    with connect() as c:return solve_molar(d,catalog(c))


@app.post('/api/lots/molar')
@app.post('/api/v1/synthesis/batches')
def create_molar_lot(d:dict,u=Depends(director)):
    with connect(True) as c:
        estimate=solve_molar(d,catalog(c))
        if estimate['configuration_missing']:raise RuleError('请先补齐工艺投料：'+'、'.join(estimate['configuration_missing']))
        route=estimate['prepared_input']['route'];ident=serial(c,'SYN',route)
        payload={'design':d,'estimate':estimate,'process_required':estimate['process_required'],'schema_version':'SDD-1.1',
                 'synthesis_path':estimate['synthesis_path'],'composition_tag':estimate['composition_tag'],'monomers':estimate['prepared_input']['monomers'],'note':str(d.get('note') or '')[:4000]}
        c.execute('INSERT INTO lots(id,route,status,payload,created) VALUES(?,?,?,?,?)',(ident,route,'SYNTHESIZING',pack(payload),now()))
        for x in estimate['molar_items']:
            c.execute('INSERT INTO synthesis_molar_items(lot_id,code,group_name,mol_pct,moles,mass,purity,stage,volume_ml) VALUES(?,?,?,?,?,?,?,?,?)',
                      (ident,x['code'],x['group'],x['mol_pct'],x.get('moles'),x['mass'],x['purity'],x['stage'],x.get('volume_ml')))
        if estimate['process_required']:
            for step in estimate['steps']:
                c.execute('INSERT INTO process_stages(lot_id,stage_no,name,status,template) VALUES(?,?,?,?,?)',(ident,step['stage'],step['title'],'PENDING',pack(step)))
        audit(c,u['name'],'CREATE_MOLAR_SYNTHESIS',ident)
    return {'id':ident}


def process_lot(c,ident):
    row=c.execute('SELECT * FROM lots WHERE id=?',(ident,)).fetchone()
    if not row:raise HTTPException(404,'批次不存在')
    payload=json.loads(row['payload'])
    if not payload.get('process_required'):raise HTTPException(409,'该批次为旧版快照或非四阶段工艺，沿用其原质检流程')
    return row,payload


@app.get('/api/lots/{ident}/process')
def process_status(ident:str,u=Depends(director)):
    with connect() as c:
        lot,payload=process_lot(c,ident);stages=[]
        for row in c.execute('SELECT * FROM process_stages WHERE lot_id=? ORDER BY stage_no',(ident,)):
            item=dict(row)
            for key in ('template','start_record','completion_record'):item[key]=json.loads(item[key]) if item[key] else None
            stages.append(item)
        workup=c.execute('SELECT * FROM workup_logs WHERE lot_id=?',(ident,)).fetchone()
        return dict(id=ident,status=lot['status'],stages=stages,workup=dict(workup) if workup else None,composition_tag=payload.get('composition_tag'))


def stage_for_write(c,ident,number):
    lot,payload=process_lot(c,ident)
    if lot['status']!='SYNTHESIZING':raise HTTPException(409,'仅合成中批次可记录工艺阶段')
    if not 1<=number<=4:raise RuleError('阶段号必须为1–4')
    row=c.execute('SELECT * FROM process_stages WHERE lot_id=? AND stage_no=?',(ident,number)).fetchone()
    if not row:raise HTTPException(404,'阶段不存在')
    if c.execute("SELECT count(*) FROM process_stages WHERE lot_id=? AND stage_no<? AND status!='COMPLETED'",(ident,number)).fetchone()[0]:
        raise RuleError('前置阶段尚未完成，不能跳步')
    return lot,payload,row


@app.post('/api/lots/{ident}/stages/{number}/start')
def start_stage(ident:str,number:int,d:dict,u=Depends(director)):
    with connect(True) as c:
        lot,payload,row=stage_for_write(c,ident,number)
        if row['status']!='PENDING':raise HTTPException(409,'阶段已开始或已完成，不可重复开始')
        record=validate_stage(number,d)
        c.execute("UPDATE process_stages SET status='RUNNING',started_at=?,start_record=?,actor=? WHERE lot_id=? AND stage_no=?",(now(),pack(record),u['name'],ident,number))
        audit(c,u['name'],'START_PROCESS_STAGE',f'{ident}/{number}')
    return {'ok':True}


@app.post('/api/lots/{ident}/stages/{number}/complete')
@app.patch('/api/v1/synthesis/batches/{ident}/stages/{number}/complete')
def complete_stage(ident:str,number:int,d:dict,u=Depends(director)):
    with connect(True) as c:
        lot,payload,row=stage_for_write(c,ident,number)
        if row['status']!='RUNNING':raise HTTPException(409,'请先通过门禁开始本阶段；已完成记录不可覆盖')
        record=validate_stage(number,d,complete=True)
        if number==4:
            theo=payload['estimate']['theoretical_resin_g'];actual=record['actual_dry_yield_g']
            yield_pct=100*actual/theo
            record['isolated_yield_pct']=yield_pct;record['theoretical_dry_yield_g']=theo
            c.execute('INSERT INTO workup_logs(lot_id,wash_cycles_count,dry_temp_min_c,dry_temp_max_c,dry_hours,theoretical_g,actual_g,yield_pct,record,actor,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (ident,record['wash_cycles_count'],record['actual_temp_min_c'],record['actual_temp_max_c'],record['hold_hours'],theo,actual,yield_pct,pack(record),u['name'],now()))
        c.execute("UPDATE process_stages SET status='COMPLETED',completed_at=?,completion_record=?,actor=? WHERE lot_id=? AND stage_no=?",(now(),pack(record),u['name'],ident,number))
        if number==4:c.execute("UPDATE lots SET status='QC_PENDING' WHERE id=?",(ident,))
        audit(c,u['name'],'COMPLETE_PROCESS_STAGE',f'{ident}/{number}')
    return {'ok':True,'qc_pending':number==4}


@app.patch('/api/v1/synthesis/batches/{ident}/release')
def release_v11(ident:str,d:dict,u=Depends(director)):
    return transition(ident,dict(d,status='RELEASED'),u)


# User-defined reaction methods: versioned templates and immutable batch snapshots.
def method_text(value,label,limit=200,required=True):
    if not isinstance(value,str) or len(value.strip())>limit or (required and not value.strip()):raise RuleError(label+'格式不正确')
    return value.strip()


def validate_method(d,cat):
    name=method_text(d.get('name'),'方法名称',100)
    route=d.get('route')
    if route not in ('N','P'):raise RuleError('请选择适用路线')
    stages=d.get('stages')
    if not isinstance(stages,list) or not 1<=len(stages)<=12:raise RuleError('方法需包含1–12个步骤')
    checked=[]
    for i,s in enumerate(stages,1):
        if not isinstance(s,dict):raise RuleError('步骤格式错误')
        lo=num(s.get('temp_min_c'),'最低温度',lo=-100,hi=250,zero=True)
        hi=num(s.get('temp_max_c'),'最高温度',lo=-100,hi=250,zero=True)
        if lo>hi:raise RuleError('步骤最低温度不能高于最高温度')
        inputs=s.get('inputs',[])
        if not isinstance(inputs,list) or len(inputs)>50:raise RuleError('每步原料最多50行')
        rows=[];seen=set()
        for x in inputs:
            if not isinstance(x,dict):raise RuleError('原料行格式错误')
            m=material(cat,x.get('code'),route=route)
            if m['code'] in seen:raise RuleError('同一步骤重复原料请合并')
            seen.add(m['code']);rows.append(dict(code=m['code'],mass=num(x.get('mass'),'计划实称质量 g'),purity=num(x.get('purity',100),'纯度%',hi=100)))
        checked.append(dict(number=i,name=method_text(s.get('name'),'步骤名称',100),temp_min_c=lo,temp_max_c=hi,
            hold_hours=num(s.get('hold_hours'),'最低持续时长 h',hi=10000,zero=True),
            apparatus=method_text(s.get('apparatus',''),'装置配置',500,False),note=method_text(s.get('note',''),'操作要求',3000,False),inputs=rows))
    return dict(name=name,route=route,description=method_text(d.get('description',''),'方法说明',3000,False),stages=checked,calculation_mode='MANUAL_MASS')


def method_view(row):
    return dict(id=row['id'],version=row['version'],enabled=bool(row['enabled']),updated=row['updated'],**json.loads(row['payload']))


@app.get('/api/reaction-methods')
def get_methods(u=Depends(director)):
    with connect() as c:return [method_view(r) for r in c.execute('SELECT * FROM reaction_methods ORDER BY updated DESC,id DESC')]


@app.post('/api/reaction-methods')
def save_method(d:dict,u=Depends(director)):
    with connect(True) as c:
        data=validate_method(d,catalog(c));ident=d.get('id')
        if ident:
            old=c.execute('SELECT * FROM reaction_methods WHERE id=?',(ident,)).fetchone()
            if not old:raise HTTPException(404,'方法不存在')
            if d.get('version')!=old['version']:raise HTTPException(409,'方法已被修改，请重新打开')
            version=old['version']+1
            c.execute('UPDATE reaction_methods SET payload=?,version=?,updated=? WHERE id=?',(pack(data),version,now(),ident))
        else:
            ident='MTH-'+secrets.token_hex(6);version=1
            c.execute('INSERT INTO reaction_methods(id,version,enabled,payload,updated) VALUES(?,?,1,?,?)',(ident,version,pack(data),now()))
        c.execute('INSERT INTO reaction_method_versions(method_id,version,payload,created,actor) VALUES(?,?,?,?,?)',(ident,version,pack(data),now(),u['name']))
        audit(c,u['name'],'SAVE_REACTION_METHOD',ident+'/'+str(version))
    return dict(id=ident,version=version)


@app.post('/api/reaction-methods/{ident}/enabled')
def enable_method(ident:str,d:dict,u=Depends(director)):
    if type(d.get('enabled')) is not bool:raise RuleError('启用状态须为布尔值')
    with connect(True) as c:
        if not c.execute('UPDATE reaction_methods SET enabled=?,updated=? WHERE id=?',(int(d['enabled']),now(),ident)).rowcount:raise HTTPException(404,'方法不存在')
        audit(c,u['name'],'ENABLE_METHOD' if d['enabled'] else 'DISABLE_METHOD',ident)
    return {'ok':True}


def method_plan(c,ident,d):
    row=c.execute('SELECT * FROM reaction_methods WHERE id=?',(ident,)).fetchone()
    if not row:raise HTTPException(404,'方法不存在')
    if not row['enabled']:raise RuleError('方法已停用')
    if d.get('version')!=row['version']:raise HTTPException(409,'模板版本已更新，请重新生成设计页')
    template=method_view(row);data=validate_method(dict(template,stages=d.get('stages')),catalog(c))
    # Batch may adjust weights and conditions, but cannot silently change method structure or its route.
    if len(data['stages'])!=len(template['stages']):raise RuleError('步骤数量与模板不符')
    for stage,base in zip(data['stages'],template['stages']):
        if stage['name']!=base['name']:raise RuleError('步骤名称与模板不符，请先编辑方法')
    if not any(x['inputs'] for x in data['stages']):raise RuleError('请至少添加一行实际计划投料')
    total=sum(x['mass'] for s in data['stages'] for x in s['inputs'])
    return dict(method=template,stages=data['stages'],total_feed_g=total,note=method_text(d.get('note',''),'批次备注',4000,False))


@app.post('/api/reaction-methods/{ident}/preview')
def preview_method(ident:str,d:dict,u=Depends(director)):
    with connect() as c:return method_plan(c,ident,d)


@app.post('/api/reaction-methods/{ident}/batches')
def create_method_batch(ident:str,d:dict,u=Depends(director)):
    with connect(True) as c:
        plan=method_plan(c,ident,d);route=plan['method']['route'];lot=serial(c,'SYN',route)
        payload=dict(method_required=True,method_snapshot=plan['method'],method_plan=plan,composition_tag=plan['method']['name']+' v'+str(plan['method']['version']),note=plan['note'])
        c.execute('INSERT INTO lots(id,route,status,payload,created) VALUES(?,?,?,?,?)',(lot,route,'SYNTHESIZING',pack(payload),now()))
        for step in plan['stages']:
            c.execute('INSERT INTO method_batch_stages(lot_id,number,status,plan) VALUES(?,?,?,?)',(lot,step['number'],'PENDING',pack(step)))
        audit(c,u['name'],'CREATE_METHOD_BATCH',lot)
    return {'id':lot}


@app.get('/api/lots/{ident}/method-process')
def method_process(ident:str,u=Depends(director)):
    with connect() as c:
        lot=c.execute('SELECT * FROM lots WHERE id=?',(ident,)).fetchone()
        if not lot:raise HTTPException(404,'批次不存在')
        payload=json.loads(lot['payload'])
        if not payload.get('method_required'):raise RuleError('该批次不属于自定义方法')
        stages=[]
        for r in c.execute('SELECT * FROM method_batch_stages WHERE lot_id=? ORDER BY number',(ident,)):
            x=dict(r);x['plan']=json.loads(x['plan']);x['record']=json.loads(x['record']) if x['record'] else None;stages.append(x)
        return dict(id=ident,status=lot['status'],method=payload['method_snapshot'],stages=stages)


@app.post('/api/lots/{ident}/method-stages/{number}/complete')
def complete_method_stage(ident:str,number:int,d:dict,u=Depends(director)):
    with connect(True) as c:
        lot=c.execute('SELECT * FROM lots WHERE id=?',(ident,)).fetchone()
        if not lot:raise HTTPException(404,'批次不存在')
        if lot['status']!='SYNTHESIZING':raise HTTPException(409,'仅合成中批次可完成步骤')
        row=c.execute('SELECT * FROM method_batch_stages WHERE lot_id=? AND number=?',(ident,number)).fetchone()
        if not row:raise HTTPException(404,'步骤不存在')
        if row['status']=='COMPLETED':raise HTTPException(409,'已完成记录不可覆盖')
        if c.execute("SELECT 1 FROM method_batch_stages WHERE lot_id=? AND number<? AND status!='COMPLETED'",(ident,number)).fetchone():raise RuleError('前置步骤未完成')
        plan=json.loads(row['plan']);lo=num(d.get('temp_min_c'),'实测最低温度',lo=-100,hi=250,zero=True);hi=num(d.get('temp_max_c'),'实测最高温度',lo=-100,hi=250,zero=True)
        hold=num(d.get('hold_hours'),'实际持续时长',hi=10000,zero=True)
        if lo>hi or lo<plan['temp_min_c'] or hi>plan['temp_max_c']:raise RuleError('实测温度不满足本批次计划范围')
        if hold<plan['hold_hours']:raise RuleError('实际持续时长未达到本批次计划')
        if d.get('confirmed') is not True:raise RuleError('请核对投料、装置与操作要求后确认')
        record=dict(temp_min_c=lo,temp_max_c=hi,hold_hours=hold,note=method_text(d.get('note',''),'过程记录',4000,False),confirmed=True)
        c.execute("UPDATE method_batch_stages SET status='COMPLETED',record=?,completed=?,actor=? WHERE lot_id=? AND number=?",(pack(record),now(),u['name'],ident,number))
        remains=c.execute("SELECT count(*) FROM method_batch_stages WHERE lot_id=? AND status!='COMPLETED'",(ident,)).fetchone()[0]
        if not remains:c.execute("UPDATE lots SET status='QC_PENDING' WHERE id=?",(ident,))
        audit(c,u['name'],'COMPLETE_METHOD_STAGE',ident+'/'+str(number))
    return {'ok':True,'qc_pending':not remains}


app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
@app.get('/')
def index(): return FileResponse(ROOT/'static'/'index.html')
