"""Pure calculations. No persistence, no client-supplied molecular weights."""
import math

class RuleError(ValueError): pass

def num(value, label, lo=0, hi=None, zero=False):
    if isinstance(value, bool): raise RuleError(f'{label}必须为数字')
    try: x=float(value)
    except (TypeError, ValueError): raise RuleError(f'{label}必须为数字') from None
    if not math.isfinite(x) or x < lo or (not zero and x == lo) or x>1e15 or (hi is not None and x>hi):
        raise RuleError(f'{label}超出有效范围')
    return x

def material(catalog, code, groups=None, route=None):
    if code not in catalog: raise RuleError(f'未知物料：{code}')
    m=catalog[code]
    if groups and m['category'] not in groups: raise RuleError(f'{code}类别不匹配')
    if route and m['route'] not in (route,'BOTH'): raise RuleError(f'{code}不适用于此路线')
    return m

# Molecular weights below use the same two-decimal convention as the catalog.
HCL_MW=36.46
SO2_MW=64.06
HYDROXY_SIDECHAINS={'GR-01','GR-04'}
PATH_NAMES={'PAA_DIRECT':'PAA直接聚加','POST_GRAFT':'PAA聚合后侧链接枝','PAE_ACYL_CHLORIDE':'PAE预酯化—酰氯化—缩聚','PHA_DIRECT':'PBO/PHA直接缩聚'}


def monomer_feed(d,catalog):
    route=d.get('route')
    if route not in ('N','P'): raise RuleError('路线必须为N或P')
    groups=('DA','DM') if route=='N' else ('PBO-DC','PBO-AP')
    totals={groups[0]:0.,groups[1]:0.};dry=0.;dar=0.;rows=[];seen=set();end=0.
    source=d.get('monomers',[])
    if not isinstance(source,list) or len(source)>100: raise RuleError('单体投料必须是最多100项的列表')
    for row in source:
        if not isinstance(row,dict): raise RuleError('单体行格式错误')
        code=row.get('code');m=material(catalog,code,[*groups,'EC'],route)
        if code in seen: raise RuleError('单体不可重复，请合并称量')
        seen.add(code)
        mass=num(row.get('mass'),'投料质量');purity=num(row.get('purity',100),'纯度%',hi=100)/100
        n=mass*purity/m['mw']
        if m['category']=='EC':end+=n
        else:totals[m['category']]+=n
        dry+=n*(140.14 if code=='PBO-AP07' else m['mw'])
        if code=='PBO-AP07':dar+=n
        rows.append(dict(code=code,mass=mass,purity=purity*100,moles=n,category=m['category']))
    a,b=(totals[g] for g in groups)
    if a<=0 or b<=0:raise RuleError('必须同时加入两类双官能单体')
    return groups,rows,a,b,dry,dar,end


def stoichiometry_settings(d):
    mode=d.get('stoichiometry_mode','ACTUAL')
    if mode not in ('ACTUAL','BALANCED','A_EXCESS','B_EXCESS'):raise RuleError('未知的过量比模式')
    pct=num(d.get('excess_pct',0),'目标过量百分比',hi=100,zero=True)
    if mode=='BALANCED':pct=0.
    return mode,pct


def balance_feed(d,catalog):
    groups,rows,a,b,*_=monomer_feed(d,catalog)
    mode,pct=stoichiometry_settings(d)
    if mode=='ACTUAL':raise RuleError('请先选择等摩尔、A过量或B过量模式')
    ratio=1+pct/100
    group=groups[0] if mode=='A_EXCESS' else groups[1]
    factor=(b*ratio/a) if mode=='A_EXCESS' else (a*(ratio if mode=='B_EXCESS' else 1)/b)
    adjusted=[dict(code=x['code'],mass=x['mass']*(factor if x['category']==group else 1),purity=x['purity']) for x in rows]
    return dict(monomers=adjusted,adjusted_group=group,scale=factor,stoichiometry_mode=mode,excess_pct=pct)


def synthesis(d,catalog):
    route=d.get('route');groups,rows,a,b,dry,dar,end=monomer_feed(d,catalog)
    path=d.get('synthesis_path') or ('PHA_DIRECT' if route=='P' else 'POST_GRAFT' if d.get('graft_code') else 'PAA_DIRECT')
    if path not in PATH_NAMES or (route=='P' and path!='PHA_DIRECT') or (route=='N' and path=='PHA_DIRECT'):raise RuleError('合成路线与聚合物类型不匹配')
    mode,pct=stoichiometry_settings(d)
    target=1+pct/100
    actual_ratio=a/b if mode=='A_EXCESS' else b/a
    if mode!='ACTUAL' and abs(actual_ratio-target)>1e-4*target:
        raise RuleError('当前投料与所选过量比不一致，请点击“按比例调整投料”或切换为按实际投料计算')
    p=num(d.get('conversion',.995),'反应转化率',hi=1)
    r=min(a,b)/max(a,b);den=1+r-2*r*p;xn=(1+r)/den if den>1e-12 else None
    actual_side='BALANCED' if abs(a-b)<1e-10*max(a,b) else 'A_EXCESS' if a>b else 'B_EXCESS'
    actual_excess=0. if actual_side=='BALANCED' else (max(a,b)/min(a,b)-1)*100
    byproduct=2*min(a,b)*HCL_MW if route=='P' else 0.
    dry-=byproduct;graft_moles=0.;graft_mass=0.;steps=[];sidechain='';acyl_mass=0.;warnings=[]
    if path=='POST_GRAFT' and not d.get('graft_code'):raise RuleError('聚合后接枝路线必须选择侧链试剂')
    if path=='PAA_DIRECT' and d.get('graft_code'):raise RuleError('直接PAA路线不含侧链接枝，请选择后接枝或PAE分步路线')
    if path=='PAE_ACYL_CHLORIDE':
        code=d.get('graft_code')
        if code not in HYDROXY_SIDECHAINS:raise RuleError('预酯化路线仅支持羟基型HEMA或HEA；GMA/MOI应使用后接枝路线')
        g=material(catalog,code,['GR'],'N');alpha=num(d.get('graft_degree',1),'二酯化位点比例',hi=1)
        if abs(alpha-1)>1e-9:raise RuleError('本模型要求二酐两个环均开环形成二酯；部分酯化混合体系暂不作同一模型估算')
        side_pct=num(d.get('sidechain_excess_pct',0),'羟基侧链试剂过量%',hi=100,zero=True)
        side_purity=num(d.get('sidechain_purity',100),'侧链试剂纯度%',hi=100)/100
        graft_moles=2*a;needed_side=graft_moles*(1+side_pct/100)
        graft_mass=needed_side*g['mw']/side_purity
        explicit_side=d.get('sidechain_mass')
        if explicit_side not in (None,''):graft_mass=num(explicit_side,'侧链试剂实际投料')
        side_feed_mol=graft_mass*side_purity/g['mw']
        if side_feed_mol<graft_moles*(1-1e-8):raise RuleError('羟基侧链试剂不足，不能按完全二酯化模型计算')
        reagent=material(catalog,'RG-01',['RG'],'N')
        eq=num(d.get('acyl_eq',1),'SOCl₂/COOH当量',lo=0,hi=100)
        if eq<1:raise RuleError('本模型按羧基完全酰氯化估算，SOCl₂/COOH当量不得小于1')
        purity=num(d.get('acyl_purity',100),'二氯亚砜纯度%',hi=100)/100
        acid_sites=2*a;acyl_mass=acid_sites*eq*reagent['mw']/purity
        if d.get('acyl_mass') not in (None,''):acyl_mass=num(d['acyl_mass'],'二氯亚砜实际投料')
        acyl_moles=acyl_mass*purity/reagent['mw']
        if acyl_moles<acid_sites*(1-1e-8):raise RuleError('二氯亚砜实际投料不足，不能按完全酰氯化模型计算')
        acid_mass=sum(x['mass']*x['purity']/100 for x in rows if x['category']==groups[0])+graft_moles*g['mw']
        acid_chloride_mass=acid_mass+acid_sites*(reagent['mw']-SO2_MW-HCL_MW)
        amide_hcl=2*min(a,b)*HCL_MW
        dry+=graft_moles*g['mw']+acid_sites*(reagent['mw']-SO2_MW-HCL_MW)-amide_hcl
        byproduct=acid_sites*HCL_MW+amide_hcl
        sidechain='甲基丙烯酸酯侧链' if code=='GR-01' else '丙烯酸酯侧链'
        def note(key):
            value=str(d.get(key) or '')
            if len(value)>4000:raise RuleError('阶段记录最长4000字符')
            return value
        steps=[dict(stage=1,title='二酐与羟基侧链试剂预酯化',inputs=[dict(code=x['code'],mass=x['mass']) for x in rows if x['category']==groups[0]]+[dict(code=code,mass=graft_mass)],
                    effective_sidechain_feed_mol=side_feed_mol,incorporated_sidechain_mol=graft_moles,actual_excess_pct=(side_feed_mol/graft_moles-1)*100,
                    theoretical_intermediate_g=acid_mass,HCl_g=0.,SO2_g=0.,note=note('step1_note')),
               dict(stage=2,title='羧基酰氯化（SOCl₂）',inputs=[dict(code='RG-01',mass=acyl_mass)],carboxyl_mol=acid_sites,actual_equivalents=acyl_moles/acid_sites,
                    theoretical_intermediate_g=acid_chloride_mass,HCl_g=acid_sites*HCL_MW,SO2_g=acid_sites*SO2_MW,note=note('step2_note')),
               dict(stage=3,title='二酰氯中间体与二胺缩聚',inputs=[dict(code=x['code'],mass=x['mass']) for x in rows if x['category'] in (groups[1],'EC')],
                    HCl_g=amide_hcl,SO2_g=0.,theoretical_intermediate_g=dry,note=note('step3_note'))]
        warnings.append('按二酯化、酰氯化完全完成及缩聚限制单体完全反应估算；过量HEMA/HEA、SOCl₂与杂质不计入理论树脂。侧链试剂和酰氯化试剂实际量覆盖当量建议值。')
        warnings.append('阶段副产物按化学计量显示，未计算捕酸剂、溶剂、分离损耗及过量单体去除；不能用作实际收率或实测固含。')
    elif d.get('graft_code'):
        if route!='N':raise RuleError('PBO直接缩聚路线不含本PSPI侧链模型')
        code=d['graft_code']
        if code not in ('GR-01','GR-02','GR-03','GR-04'):raise RuleError('该侧链试剂尚未定义质量增量模型')
        g=material(catalog,code,['GR'],'N');alpha=num(d.get('graft_degree',0),'接枝率',hi=1,zero=True)
        graft_moles=2*a*alpha;graft_mass=graft_moles*g['mw']
        dry+=graft_moles*(g['mw']-(18.02 if code in HYDROXY_SIDECHAINS else 0))
        sidechain={'GR-01':'HEMA酯化侧链','GR-02':'GMA环氧开环侧链','GR-03':'MOI加成侧链','GR-04':'HEA酯化侧链'}[code]
        warnings.append('后接枝沿用2nAα简化位点模型；请根据实际可反应羧基/羟基确认位点数量。')
    if dry<=0:raise RuleError('理论树脂质量非正，请核对投料')
    if a>b:warnings.append('A组分过量时现有酸酐型封端剂不用于封闭酸端，封端需求显示0；酸端封端体系需单独确认。')
    return dict(nA=a,nB=b,r=r,Xn=xn,endcap_required_mol=2*max(b-a,0),endcap_actual_mol=end,
                theoretical_resin_g=dry,HCl_generated_g=byproduct,DAR_extra_base_mol=2.05*dar,graft_moles=graft_moles,graft_feed_g=graft_mass,
                monomers=rows,synthesis_path=path,path_name=PATH_NAMES[path],sidechain=sidechain,acyl_feed_g=acyl_mass,steps=steps,
                stoichiometry_mode=mode,target_excess_pct=pct,actual_excess_side=actual_side,actual_excess_pct=actual_excess,warnings=warnings,
                method='双官能Carothers估算；转化率仅用于Xn估算，理论投料/树脂及副产物按对应完全反应模型。封端需求仅按B过量列示，不作为实测Mn；树脂质量未计盐、杂质、损耗和后续环化。')


def formulation(d, lots, catalog):
    route=d.get('route')
    if route not in ('N','P'): raise RuleError('路线必须为N或P')
    w=num(d.get('resin_g'),'树脂干重'); target=num(d.get('target_sc'),'目标固含%',hi=100)/100
    if d.get('basis','TOTAL') not in ('TOTAL','RESIN'): raise RuleError('未知固含基准')
    selected=d.get('lots',[])
    if not selected: raise RuleError('请至少选择一个已放行批次')
    if len({x.get('id') for x in selected})!=len(selected): raise RuleError('不可重复选择批次')
    ratios=[num(x.get('ratio'),'混批干树脂占比%',hi=100) for x in selected]
    if abs(sum(ratios)-100)>1e-6: raise RuleError('混批干树脂占比必须合计100%')
    carried={}; lines=[]; snapshots=[]; dry=w; existing=0.
    def carrier(code,mass):
        material(catalog,code,['SV']); carried[code]=carried.get(code,0)+mass
    for x,ratio in zip(selected,ratios):
        lot=lots.get(x['id'])
        if not lot or lot['status']!='RELEASED' or lot['route']!=route: raise RuleError('批次未放行或路线不匹配')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if lot.get('expiry') and lot['expiry']<datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(): raise RuleError('批次已过有效期')
        sc=num(lot['qc']['sc'],'批次固含%',hi=100)/100
        resin=w*ratio/100; mass=resin/sc
        if mass-lot['stock']>1e-8: raise RuleError(f'{lot["id"]}库存不足')
        if mass-resin>1e-9:
            profile=lot['qc'].get('solvent_profile') or [{'code':lot['solvent'],'ratio':100}]
            for component in profile: carrier(component['code'],(mass-resin)*component['ratio']/100)
        lines.append(dict(code=lot['id'],kind='POLYMER',mass=mass,dry_g=resin))
        snapshots.append(dict(id=lot['id'],sc=sc*100,ratio=ratio,mass=mass,qc=lot['qc'],solvent=lot['solvent']))
        existing+=mass
    seen=set()
    for x in d.get('additives',[]):
        code=x.get('code'); material(catalog,code,['PAC','PAG','PI','PS','XL','AD','IN'],route)
        if code in seen: raise RuleError('助剂不可重复')
        seen.add(code)
        phr=num(x.get('phr'),'phr',zero=True); sc=num(x.get('sc',100),'助剂有效固含%',hi=100)/100
        solid=w*phr/100; mass=solid/sc
        if mass-solid>1e-9: carrier(x.get('solvent'),mass-solid)
        lines.append(dict(code=code,kind='ADDITIVE',mass=mass,dry_g=solid,phr=phr,sc=sc*100))
        dry+=solid; existing+=mass
    numerator=w if d.get('basis')=='RESIN' else dry
    total=numerator/target
    if total<existing-1e-8: raise RuleError('目标固含无法仅靠稀释达到，请降低目标固含或提高原液固含')
    mix=d.get('solvents',[])
    if not mix: raise RuleError('请选择最终溶剂配比')
    if len({x.get('code') for x in mix})!=len(mix): raise RuleError('溶剂不可重复')
    fracs=[num(x.get('ratio'),'最终溶剂占比%',hi=100,zero=True) for x in mix]
    if abs(sum(fracs)-100)>1e-6: raise RuleError('最终溶剂占比必须合计100%')
    if set(carried)-{x['code'] for x in mix}: raise RuleError('目标溶剂配比未包含原液携带的溶剂')
    for x,f in zip(mix,fracs):
        code=x.get('code'); material(catalog,code,['SV']); mass=(total-dry)*f/100-carried.get(code,0)
        if mass< -1e-7: raise RuleError(f'{code}已携带量超过目标量，无法通过加料实现，请调整配比')
        lines.append(dict(code=code,kind='SOLVENT',mass=max(0,mass),dry_g=0))
    if abs(sum(x['mass'] for x in lines)-total)>1e-6: raise RuleError('质量平衡未闭合')
    for x in lines: x['wt_pct']=100*x['mass']/total
    return dict(lines=lines,lots=snapshots,total_g=total,dry_g=dry,resin_g=w,
                total_solid_pct=100*dry/total,resin_solid_pct=100*w/total,
                new_solvent_g=total-existing,carried_solvents=carried,route=route,basis=d.get('basis','TOTAL'))
