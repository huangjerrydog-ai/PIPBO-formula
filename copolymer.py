"""SDD v1.1 molar feed solver; all chemistry data comes from the server catalog."""
from engine import RuleError, num, material, synthesis

DENSITIES={'CAT-01':1.018,'CL-01':1.631,'AS-01':.982}
STAGES=[
    dict(stage=1,title='开环酯化',target_temp_min_c=None,target_temp_max_c=None,hold_hours=24,apparatus='三口瓶 / N₂保护',dosing_method='DIRECT_CHARGE'),
    dict(stage=2,title='低温酰氯化',target_temp_min_c=0,target_temp_max_c=10,hold_hours=2,apparatus='冰浴 / 恒压滴液漏斗 / 尾气吸收',dosing_method='DROPWISE_FUNNEL'),
    dict(stage=3,title='低温缩聚与室温续反应',target_temp_min_c=0,target_temp_max_c=10,hold_hours=2,post_rt_hours=2,apparatus='冰浴 / 预混液滴加',dosing_method='PREMIX_SOLUTION'),
    dict(stage=4,title='沉淀洗涤与真空干燥',target_temp_min_c=40,target_temp_max_c=60,hold_hours=24,apparatus='纯水沉淀 / 乙醇水洗涤 / 真空干燥',dosing_method='WORKUP')]


def normalize_group(source,category,route,catalog,fraction_mode):
    if not isinstance(source,list) or not 1<=len(source)<=50:raise RuleError('A/B每组需选择1–50个单体')
    result=[];seen=set()
    for row in source:
        if not isinstance(row,dict):raise RuleError('摩尔配比行格式错误')
        code=row.get('code',row.get('internal_code'));m=material(catalog,code,[category],route)
        if code in seen:raise RuleError('同组单体不能重复，请合并配比')
        seen.add(code)
        ratio=num(row.get('ratio',row.get('molar_equiv_pct')),'组内摩尔配比')
        purity=num(row.get('purity',row.get('purity_pct',100)),'单体纯度%',hi=100)
        result.append(dict(code=code,ratio=ratio,purity=purity,mw=m['mw']))
    total=sum(x['ratio'] for x in result)
    if fraction_mode=='PERCENT' and abs(total-100)>.01000001:raise RuleError('百分比模式下A、B两组各自须合计100 mol%（容差±0.01）')
    for row in result:row['mol_pct']=row['ratio']/total*100
    return result


def optional_mass(params,key,label,missing):
    value=params.get(key)
    if value in (None,''):
        missing.append(label);return None
    return num(value,label)


def solve_molar(d,catalog):
    route=d.get('route',{'NEGATIVE_PSPI':'N','POSITIVE_PBO':'P'}.get(d.get('resist_route')))
    if route not in ('N','P'):raise RuleError('请选择PSPI或PBO路线')
    precursor=d.get('precursor_type','PAE_3STEP' if route=='N' else 'PHA_PBO')
    paths={'PAE_3STEP':'PAE_ACYL_CHLORIDE','PAA':'PAA_DIRECT','PHA_PBO':'PHA_DIRECT'}
    if precursor not in paths or (route=='P')!=(precursor=='PHA_PBO'):raise RuleError('前驱体类型与聚合物路线不匹配')
    fraction_mode=d.get('fraction_mode','RATIO')
    if fraction_mode not in ('RATIO','PERCENT'):raise RuleError('未知的组内配比模式')
    ga=normalize_group(d.get('group_a',d.get('group_a_monomers')), 'DA' if route=='N' else 'PBO-DC',route,catalog,fraction_mode)
    gb=normalize_group(d.get('group_b',d.get('group_b_monomers')), 'DM' if route=='N' else 'PBO-AP',route,catalog,fraction_mode)
    r=num(d.get('r_ratio',.98),'组间r=nA/nB',hi=1)
    cap=material(catalog,d.get('endcap_code','EC-01'),['EC'],route)
    cap_purity=num(d.get('endcap_purity',100),'封端剂纯度%',hi=100)
    p=num(d.get('conversion',.995),'转化率p',hi=1)
    reagent=d.get('pae_reagent_equivs',{})
    if not isinstance(reagent,dict):raise RuleError('工艺助剂设置格式错误')
    side=d.get('graft_code','GR-01');hema_eq=num(reagent.get('hema_eq',2.05),'HEMA/二酐当量',hi=100)
    if precursor=='PAE_3STEP' and hema_eq<2:raise RuleError('完全二酯化模型要求羟基侧链试剂/二酐当量至少2')
    def feed(b_moles):
        result=[]
        for rows,moles in ((ga,r*b_moles),(gb,b_moles)):
            for row in rows:result.append(dict(code=row['code'],mass=moles*row['mol_pct']/100*row['mw']/(row['purity']/100),purity=row['purity']))
        if r<1:result.append(dict(code=cap['code'],mass=2*(1-r)*b_moles*cap['mw']/(cap_purity/100),purity=cap_purity))
        return result
    core=dict(route=route,synthesis_path=paths[precursor],conversion=p,stoichiometry_mode='ACTUAL')
    if precursor=='PAE_3STEP':
        core.update(graft_code=side,graft_degree=1,sidechain_excess_pct=(hema_eq/2-1)*100,sidechain_purity=reagent.get('hema_purity_pct',100))
    mode=d.get('drive_mode','BASE_B')
    unit=synthesis(dict(core,monomers=feed(1)),catalog)['theoretical_resin_g']
    if mode=='BASE_B':b=num(d.get('base_group_b_mmol'),'B组基准 mmol')/1000
    elif mode=='BASE_A':b=num(d.get('base_group_a_mmol'),'A组基准 mmol')/1000/r
    elif mode=='TARGET_DRY':b=num(d.get('target_dry_g'),'目标理论干重 g')/unit
    else:raise RuleError('请选择基准摩尔量或目标干重驱动模式')
    prepared=dict(core,monomers=feed(b));estimate=synthesis(prepared,catalog);a=r*b
    groups=[]
    for label,rows,total in [('GROUP_A',ga,a),('GROUP_B',gb,b)]:
        for row in rows:
            n=total*row['mol_pct']/100
            groups.append(dict(code=row['code'],group=label,mol_pct=row['mol_pct'],moles=n,mass=n*row['mw']/(row['purity']/100),purity=row['purity'],stage=3 if label=='GROUP_B' and precursor=='PAE_3STEP' else 1))
    if r<1:groups.append(dict(code=cap['code'],group='END_CAPPER',mol_pct=0,moles=2*(b-a),mass=2*(b-a)*cap['mw']/(cap_purity/100),purity=cap_purity,stage=3 if precursor=='PAE_3STEP' else 1))
    tag=' - '.join('/'.join(x['code'] for x in rows)+' ('+':'.join(f"{x['mol_pct']:.4g}" for x in rows)+')' for rows in (ga,gb))
    estimate.update(precursor_type=precursor,composition_tag=tag,r_ratio=r,r_eff=a/(2*b-a),Xn_p1=(a+b)/(b-a) if b>a else None,
                    normalized_a=ga,normalized_b=gb,molar_items=groups,drive_mode=mode,unit_mass_per_mol_b=unit,
                    process_required=precursor=='PAE_3STEP',configuration_missing=[],prepared_input=prepared)
    if precursor!='PAE_3STEP':
        estimate['steps']=[]
        if precursor=='PHA_PBO':
            py=material(catalog,'AS-01',['AS'],'P')
            n=2*a+estimate['DAR_extra_base_mol']
            mass=n*py['mw']
            groups.append(dict(code='AS-01',group='ACID_SCAVENGER',mol_pct=0,moles=n,mass=mass,purity=100,stage=1,volume_ml=mass/DENSITIES['AS-01']))
            estimate.update(py_required_mol=n,py_feed_mol=n)
        return estimate
    if 'stage_additives' in d:
        reagent=dict(reagent,dbu_mol_pct=5,py_eq='',hq1_wt_pct=.1,hq2_mass_g=.01,dbu_purity_pct=100,py_purity_pct=100)
    missing=[]
    dbu_pct=num(reagent.get('dbu_mol_pct',5),'DBU相对二酐mol%',hi=100)
    socl_eq=num(reagent.get('socl2_eq',2.1),'SOCl₂/二酐当量',hi=100)
    if socl_eq<2:raise RuleError('完全酰氯化模型要求SOCl₂/二酐当量至少2')
    hq1_pct=num(reagent.get('hq1_wt_pct',.1),'HQ1相对侧链试剂wt%',hi=100)
    def purity(key):return num(reagent.get(key,100),key,hi=100)/100
    side_m=material(catalog,side,['GR'],'N');hema_n=a*hema_eq
    hema_mass=hema_n*side_m['mw']/purity('hema_purity_pct')
    dbu=material(catalog,'CAT-01',['CAT'],'N');cl=material(catalog,'CL-01',['CL'],'N');py=material(catalog,'AS-01',['AS'],'N');material(catalog,'IN-03',['IN'],'N');material(catalog,'WP-01',['WP'],'N')
    cl_n=a*socl_eq;min_py=2*b+2*max(0,cl_n-2*a)
    py_eq=reagent.get('py_eq')
    py_n=min_py if py_eq in (None,'') else num(py_eq,'吡啶/二酐当量',hi=100)*a
    if py_n+1e-10<min_py:raise RuleError('吡啶有效量不足：须满足2nB+2(nSOCl₂−2nA)')
    hq1=hema_mass*hq1_pct/100
    hq2=optional_mass(reagent,'hq2_mass_g','HQ2实际称量 g',missing)
    nmp1=optional_mass(reagent,'nmp1_g','NMP1实际称量 g',missing);nmp2=optional_mass(reagent,'nmp2_g','NMP2实际称量 g',missing)
    def item(code,mass,moles=None,stage=1,group='REAGENT',purity_pct=100):
        volume=mass/DENSITIES[code] if code in DENSITIES and mass is not None else None
        return dict(code=code,mass=mass,moles=moles,volume_ml=volume,stage=stage,group=group,purity=purity_pct,mol_pct=0)
    extra=[item(side,hema_mass,hema_n,1,'GRAFTING',purity('hema_purity_pct')*100),item('CAT-01',a*dbu_pct/100*dbu['mw']/purity('dbu_purity_pct'),a*dbu_pct/100,1,purity_pct=purity('dbu_purity_pct')*100),
           item('IN-03',hq1,hq1/catalog['IN-03']['mw'],1),item('SV-01',nmp1,stage=1,group='SOLVENT'),
           item('CL-01',cl_n*cl['mw']/purity('socl2_purity_pct'),cl_n,2,purity_pct=purity('socl2_purity_pct')*100),
           item('AS-01',py_n*py['mw']/purity('py_purity_pct'),py_n,3,purity_pct=purity('py_purity_pct')*100),
           item('IN-03',hq2,None if hq2 is None else hq2/catalog['IN-03']['mw'],3),item('SV-01',nmp2,stage=3,group='SOLVENT')]
    if 'stage_additives' in d:
        rows=d['stage_additives']
        if not isinstance(rows,list) or len(rows)>100:raise RuleError('阶段助剂必须为列表，最多100行')
        extra=[x for x in extra if x['code'] not in ('CAT-01','IN-03','AS-01')]
        missing=[x for x in missing if not x.startswith('HQ2')]
        py_n=0;seen=set()
        for row in rows:
            if not isinstance(row,dict):raise RuleError('阶段助剂行格式错误')
            stage=num(row.get('stage'),'助剂阶段',hi=4)
            if not stage.is_integer():raise RuleError('助剂阶段须为1–4整数')
            stage=int(stage);code=row.get('code')
            m=material(catalog,code,['CAT','AS','IN','AD','WP'],'N')
            if (stage,code) in seen:raise RuleError('同一阶段的相同助剂请合并为一行')
            seen.add((stage,code));pct=num(row.get('purity',100),'助剂纯度%',hi=100)
            mode=row.get('mode','MASS');value=row.get('value')
            if mode=='AUTO_PY':
                if code!='AS-01' or stage!=3:raise RuleError('自动吸酸配平仅适用于阶段3的Py')
                n=min_py;mass=n*m['mw']/(pct/100)
            else:
                if value in (None,''):
                    missing.append(f'Stage {stage} {code}用量');extra.append(item(code,None,stage=stage,purity_pct=pct));continue
                value=num(value,'助剂用量')
                if mode=='MASS':mass=value;n=mass*pct/100/m['mw']
                elif mode in ('MOL_EQ','MOL_PERCENT'):
                    n=a*value/(100 if mode=='MOL_PERCENT' else 1);mass=n*m['mw']/(pct/100)
                elif mode=='HEMA_WT_PERCENT':mass=hema_mass*value/100;n=mass*pct/100/m['mw']
                else:raise RuleError('未知的助剂计量方式')
            if code=='AS-01' and stage==3:py_n+=n
            entry=item(code,mass,n,stage,purity_pct=pct);entry.update(dosage_mode=mode,dosage_value=value)
            extra.append(entry)
        if py_n+1e-10<min_py:
            if ('Stage 3 AS-01用量') not in missing:missing.append('Stage 3吸酸剂Py有效量不足，请添加Py并补足用量或自动配平')
    stages=[]
    all_items=groups+extra
    for definition in STAGES:
        step=dict(definition);step['inputs']=[x for x in all_items if x['stage']==step['stage']]
        if step['stage']==4:step['workup']='纯水沉淀；乙醇/水洗涤；40–60°C真空干燥至少24h，完成后录入实际干粉质量'
        stages.append(step)
    estimate.update(steps=stages,molar_items=all_items,graft_feed_g=hema_mass,acyl_feed_g=next(x['mass'] for x in extra if x['code']=='CL-01'),
                    py_required_mol=min_py,py_feed_mol=py_n,hema_eq=hema_eq,socl2_eq=socl_eq,configuration_missing=missing,
                    output_form='DRY_POWDER',method='SDD v1.1：A/B独立归一，r=nA/nB；净树脂质量按完全反应骨架估算，辅剂/盐/溶剂及过量侧链试剂不计入树脂。p仅用于双官能Carothers估算。')
    estimate['warnings']=['文档当量均以nA为1 eq；与旧版SOCl₂/COOH当量不同。温控和时长门禁依据人工填写记录，不连接实验仪器。']
    if not 2<=hema_eq<=2.1:estimate['warnings'].append('侧链试剂当量超出文档2.00–2.10参考范围，请研发核对。')
    if not 2.05<=socl_eq<=2.2:estimate['warnings'].append('SOCl₂当量超出文档2.05–2.20参考范围，请研发核对。')
    return estimate
