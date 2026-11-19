"""Bounded spreadsheet parsing and shared material/measurement validation."""
import csv
import io
import re
from zipfile import ZipFile, BadZipFile
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from engine import RuleError, num

MAX_BYTES=5*1024*1024
MAX_ROWS=1000
MAX_SCAN_ROWS=20000
MAX_COLUMNS=32
MATERIAL_FIELDS={
    'code':('内部编码','必填，唯一；英文字母、数字、横线、下划线，最长32字符'),
    'name':('物料名称','必填，可填英文名称/简称；真实名称仅研发管理员可见'),
    'cn_name':('中文名称','选填，建议填写中文标准名；旧模板不含此列时仍可导入'),
    'category':('类别','必填：DA, DM, PBO-AP, PBO-DC, EC, GR, PAC, PAG, PI, PS, XL, AD, IN, SV, RG, CAT, CL, AS, WP'),
    'route':('适用路线','必填：N=负性PSPI；P=正性PBO；BOTH=通用，需符合类别'),
    'mw':('分子量(g/mol)','必填，正数；PAC母核与商品分子量须区分'),
    'cas':('CAS','选填，按文本录入，不自动验证化学身份'),
    'supplier':('供应商','选填，仅研发管理员可见'),
    'grade':('商品牌号','选填，仅研发管理员可见'),
    'smiles':('SMILES','选填，仅存储文本，本版不解析结构')}
TEST_FIELDS={
    'external_id':('外部记录ID','必填，1–100字符，全系统唯一；同ID同内容重复导入跳过，不同内容报错'),
    'formula_id':('配方批号','必填，须为系统中已保存的FRM配方批号'),
    'film_um':('膜厚(μm)','选填，非负数，单位μm'),
    'dose_mj':('曝光剂量(mJ/cm²)','选填，非负数，单位mJ/cm²'),
    'resolution_um':('分辨率(μm)','选填，非负数，单位μm'),
    'residual_pct':('残膜率(%)','选填，0–100，输入95表示95%，不使用Excel百分数单元格'),
    'cure_c':('固化温度(°C)','选填，非负数，单位°C'),
    'viscosity':('粘度(mPa·s)','选填，非负数，单位mPa·s'),
    'note':('测试备注','选填，最长4000字符；测试指标至少填写一项，0视为有效值')}
SCHEMAS={'materials':MATERIAL_FIELDS,'measurements':TEST_FIELDS}
METRICS=('film_um','dose_mj','resolution_um','residual_pct','cure_c','viscosity')


def text(value,label,max_length=1000):
    value='' if value is None else str(value).strip()
    if len(value)>max_length: raise RuleError(f'{label}最长{max_length}字符')
    if '\x00' in value: raise RuleError(f'{label}不能包含空字符')
    return value


def validate_material(d):
    code=text(d.get('code'),'内部编码',32).upper()
    if not re.fullmatch(r'[A-Z0-9_-]+',code): raise RuleError('内部编码仅可用英文字母、数字、横线及下划线')
    category=text(d.get('category'),'类别').upper()
    route=text(d.get('route'),'适用路线').upper()
    if category not in ('DA','DM','PBO-AP','PBO-DC','EC','GR','PAC','PAG','PI','PS','XL','AD','IN','SV','RG','CAT','CL','AS','WP'): raise RuleError('物料类别不合法')
    expected='N' if category in ('DA','DM','GR','PI','PS','IN','CAT','CL') else 'P' if category in ('PBO-AP','PBO-DC','PAC','PAG') else None
    if route not in ('N','P','BOTH') or (expected and route!=expected): raise RuleError('类别与适用路线不匹配')
    identity={k:text(d.get(k),label) for k,label in [('name','物料名称'),('cn_name','中文名称'),('cas','CAS'),('supplier','供应商'),('grade','商品牌号'),('smiles','SMILES')]}
    if not identity['name']: raise RuleError('物料名称必填')
    if not identity['cn_name'] and re.search(r'[\u4e00-\u9fff]',identity['name']): identity['cn_name']=identity['name']
    return dict(code=code,category=category,route=route,mw=num(d.get('mw'),'分子量'),identity=identity)


def validate_measurement(d,imported=False):
    ident=text(d.get('formula_id'),'配方批号',100)
    if not ident: raise RuleError('配方批号必填')
    result={k:num(d[k],TEST_FIELDS[k][0],zero=True) for k in METRICS if d.get(k) not in (None,'')}
    if not result: raise RuleError('至少填写一项测试指标（0为有效值，空白不计）')
    if result.get('residual_pct',0)>100: raise RuleError('残膜率必须为0–100，输入95表示95%')
    result['note']=text(d.get('note'),'测试备注',4000)
    data={'formula_id':ident,'values':result}
    if imported:
        ext=text(d.get('external_id'),'外部记录ID',100)
        if not ext or any(ord(c)<32 for c in ext): raise RuleError('外部记录ID必填且不能包含控制字符')
        data['external_id']=ext
    return data


def normalized_header(value):
    return re.sub(r'[\s_()（）·/%²³°℃μµ]', '',str(value or '').lower())


def aliases(kind):
    schema=SCHEMAS[kind]
    result={normalized_header(v):k for k,info in schema.items() for v in (k,info[0])}
    extra={'中文名':'cn_name','原料中文名':'cn_name','英文名称':'name','英文名':'name','内部代号':'code','名称':'name','真实名称':'name','类别编码':'category','路线':'route','分子量':'mw','cas号':'cas',
           '外部记录编号':'external_id','记录id':'external_id','测试记录id':'external_id','关联配方批号':'formula_id',
           '膜厚':'film_um','曝光剂量':'dose_mj','分辨率':'resolution_um','残膜率':'residual_pct','固化温度':'cure_c','粘度':'viscosity','黏度':'viscosity','备注':'note'}
    result.update({normalized_header(k):v for k,v in extra.items() if v in schema})
    return result


def parse_file(content,filename,kind):
    if kind not in SCHEMAS: raise RuleError('不支持的导入类别')
    if not content: raise RuleError('文件为空')
    if len(content)>MAX_BYTES: raise RuleError('文件不能超过5MB')
    ext=filename.rsplit('.',1)[-1].lower()
    warnings=[]
    if ext=='xlsx':
        try:
            with ZipFile(io.BytesIO(content)) as archive:
                parts=archive.infolist()
                if len(parts)>500 or sum(x.file_size for x in parts)>20*1024*1024: raise RuleError('Excel文件解压后过大，请缩减文件')
                if any('vbaproject' in x.filename.lower() for x in parts): raise RuleError('不支持含宏的Excel文件')
            book=load_workbook(io.BytesIO(content),read_only=True,data_only=False,keep_links=False)
            try:
                sheet=book['数据'] if '数据' in book.sheetnames else book.worksheets[0]
                if len(book.sheetnames)>1: warnings.append(f'仅读取工作表“{sheet.title}”，其余工作表不导入')
                # Ignore stale worksheet dimension declarations; bound the actual iterator.
                sheet.reset_dimensions()
                rows=[]
                for i,cells in enumerate(sheet.iter_rows(),1):
                    if i>MAX_SCAN_ROWS: raise RuleError('文件行数超过20000，请删除尾部空白行或拆分文件')
                    if len(cells)>MAX_COLUMNS: raise RuleError('文件最多32列，请删除无关列')
                    values=[]
                    for cell in cells:
                        value=cell.value
                        if cell.data_type in ('f','e'): value=InvalidCell('公式或Excel错误单元格，请粘贴为数值')
                        elif cell.number_format and '%' in cell.number_format and value is not None:
                            value=InvalidCell('不接受Excel百分数格式，请改为普通数字（95表示95%）')
                        values.append(value)
                    rows.append(values)
            finally: book.close()
        except RuleError: raise
        except (BadZipFile, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuleError('无法读取Excel，请确认文件是未加密的.xlsx格式') from exc
        except Exception as exc:
            raise RuleError('Excel结构损坏或不受支持，请另存为.xlsx重试') from exc
    elif ext in ('csv','tsv'):
        decoded=None
        for encoding in ('utf-8-sig','gb18030'):
            try: decoded=content.decode(encoding); break
            except UnicodeDecodeError: continue
        if decoded is None: raise RuleError('CSV编码无法识别，请另存为UTF-8 CSV')
        if '\x00' in decoded: raise RuleError('CSV含空字符或使用UTF-16编码，请另存为UTF-8 CSV')
        delimiter='\t' if ext=='tsv' else ','
        try:
            if ext=='csv':
                first=decoded.splitlines()[0] if decoded.splitlines() else ''
                if ',' not in first and '\t' in first: delimiter='\t'
                elif ',' not in first and ';' in first: delimiter=';'
            rows=[]
            for i,row in enumerate(csv.reader(io.StringIO(decoded,newline=''),delimiter=delimiter,strict=True),1):
                if i>MAX_SCAN_ROWS: raise RuleError('文件行数超过20000，请删除尾部空白行或拆分文件')
                if len(row)>MAX_COLUMNS: raise RuleError('文件最多32列，请删除无关列')
                rows.append(row)
        except (csv.Error,OverflowError) as exc: raise RuleError('CSV格式不正确，请检查引号、分隔符及单元格长度') from exc
    else: raise RuleError('仅支持.xlsx、.csv、.tsv；旧版.xls请另存为.xlsx')
    if not rows: raise RuleError('文件为空')
    header=rows[0]
    if any(isinstance(x,InvalidCell) for x in header): raise RuleError('表头不能使用公式、错误值或百分数格式')
    while header and header[-1] in (None,''): header.pop()
    if not header: raise RuleError('第一行必须是表头，请先下载模板')
    lookup=aliases(kind); mapped=[]; seen=set()
    for cell in header:
        key=lookup.get(normalized_header(cell))
        if not key: raise RuleError(f'无法识别列“{str(cell)[:60]}”，请使用模板列名')
        if key in seen: raise RuleError(f'表头重复映射到同一字段：{key}')
        seen.add(key);mapped.append(key)
    required={'code','name','category','route','mw'} if kind=='materials' else {'external_id','formula_id'}
    missing=required-seen
    if missing: raise RuleError('缺少必填列：'+'、'.join(SCHEMAS[kind][k][0] for k in sorted(missing)))
    output=[]
    for number,row in enumerate(rows[1:],2):
        if all(x is None or (isinstance(x,str) and not x.strip()) for x in row): continue
        problem=[]
        if any(x not in (None,'') for x in row[len(mapped):]): problem.append('存在超出表头的数据列')
        value={key:row[i] if i<len(row) else None for i,key in enumerate(mapped)}
        for key,v in value.items():
            if isinstance(v,InvalidCell): problem.append(f'{SCHEMAS[kind][key][0]}：{v.reason}');value[key]=None
        output.append({'row':number,'data':value,'parse_errors':problem})
        if len(output)>MAX_ROWS: raise RuleError('每次最多1000条数据，请拆分文件')
    if not output: raise RuleError('没有可导入的数据；请在表头下一行开始填写')
    return output,warnings


class InvalidCell:
    def __init__(self,reason): self.reason=reason


def template(kind,format):
    schema=SCHEMAS[kind]
    headers=[info[0] for info in schema.values()]
    if format=='csv':
        output=io.StringIO(newline='');csv.writer(output).writerow(headers)
        return ('\ufeff'+output.getvalue()).encode('utf-8'),'text/csv; charset=utf-8'
    if format!='xlsx': raise RuleError('模板格式仅支持xlsx或csv')
    book=Workbook();sheet=book.active;sheet.title='数据';sheet.append(headers)
    sheet.freeze_panes='A2';sheet.auto_filter.ref=f'A1:{sheet.cell(1,len(headers)).column_letter}1'
    for cell in sheet[1]:
        cell.font=Font(name='Microsoft YaHei',bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='1E5B63');cell.alignment=Alignment(wrap_text=True,vertical='center')
        sheet.column_dimensions[cell.column_letter].width=24 if cell.column<7 else 32
    sheet.row_dimensions[1].height=35
    # Identifiers retain leading zeros; percentage metrics use plain numeric values.
    for col,key in enumerate(schema,1):
        for row in range(2,52): sheet.cell(row,col).number_format='0.0000' if key in METRICS or key=='mw' else '@'
    info=book.create_sheet('填写说明')
    notes=[['字段','填写规则'],*[[item[0],item[1]] for item in schema.values()],['文件限制','最多1000条数据，5MB；只读取“数据”表，否则读取第一张表；不接受公式单元格'],['保存方式','先预览；任一错误将阻止整批写入。重复物料编码跳过，不覆盖已有数据。'],['空白与零','空白表示未测，0表示实测值为0；保留原始单位，禁止使用Excel百分数格式']]
    for row in notes: info.append(row)
    info.column_dimensions['A'].width=28;info.column_dimensions['B'].width=100
    for row in info:
        for cell in row: cell.alignment=Alignment(wrap_text=True,vertical='top');cell.font=Font(name='Microsoft YaHei',size=11)
    for cell in info[1]: cell.font=Font(name='Microsoft YaHei',bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='1E5B63')
    info.freeze_panes='A2'
    stream=io.BytesIO();book.save(stream);book.close()
    return stream.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
