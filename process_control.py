"""Manual ELN stage gates. These validate records, not live laboratory sensors."""
from engine import RuleError,num


def require_true(d,key,label):
    if d.get(key) is not True:raise RuleError(label+'须确认')


def validate_stage(stage,d,complete=False):
    if not isinstance(d,dict):raise RuleError('阶段记录格式错误')
    lo=num(d.get('actual_temp_min_c'),'实测最低温度',lo=-100,hi=250,zero=True)
    hi=num(d.get('actual_temp_max_c'),'实测最高温度',lo=-100,hi=250,zero=True)
    if lo>hi:raise RuleError('最低温度不能超过最高温度')
    record={'actual_temp_min_c':lo,'actual_temp_max_c':hi}
    apparatus=str(d.get('apparatus_config') or '').strip()
    if not apparatus or len(apparatus)>500:raise RuleError('请填写设备/装置配置，最长500字符')
    record['apparatus_config']=apparatus
    require_true(d,'weighing_verified','本阶段计划称量核对');record['weighing_verified']=True
    if stage==1:
        require_true(d,'nitrogen_ready','氮气保护');record['nitrogen_ready']=True
    if stage in (2,3):
        if lo<0 or hi>10:raise RuleError('Stage 2/3实测温度必须全程在0–10°C范围内')
        if d.get('cooling_medium')!='ICE_WATER_BATH':raise RuleError('Stage 2/3须记录冰水浴')
        record['cooling_medium']='ICE_WATER_BATH'
        expected='DROPWISE_FUNNEL' if stage==2 else 'PREMIX_SOLUTION'
        if d.get('dosing_method')!=expected:raise RuleError('阶段滴加方式不符合模板')
        record['dosing_method']=expected
    if stage==2:
        require_true(d,'tailgas_ready','尾气吸收装置就绪');record['tailgas_ready']=True
    if stage==4:
        if lo<40 or hi>60:raise RuleError('本PAE模板要求真空干燥实测温度在40–60°C')
        require_true(d,'vacuum_ready','真空干燥');record['vacuum_ready']=True
    note=str(d.get('note') or '')
    if len(note)>4000:raise RuleError('阶段备注最长4000字符')
    record['note']=note
    if complete:
        hold=num(d.get('hold_hours'),'实测保温/干燥时长 h',hi=10000)
        minimum=24 if stage in (1,4) else 2
        if hold<minimum:raise RuleError(f'本阶段模板要求保温/干燥至少{minimum}h')
        record['hold_hours']=hold
        if stage in (2,3):record['dropwise_hours']=num(d.get('dropwise_hours'),'实际滴加时长 h',hi=1000,zero=True)
        if stage==3:
            rt=num(d.get('post_rt_hours'),'室温续反应时长 h',hi=1000)
            if rt<2:raise RuleError('Stage 3室温续反应至少2h')
            record['post_rt_hours']=rt
        if stage==4:
            cycles=num(d.get('wash_cycles_count'),'洗涤次数',hi=1000)
            if not cycles.is_integer():raise RuleError('洗涤次数必须为整数')
            water=num(d.get('wash_water_ml'),'洗涤用纯水总体积 mL')
            ethanol=num(d.get('wash_ethanol_ml'),'洗涤用乙醇总体积 mL')
            require_true(d,'precipitation_confirmed','高速搅拌纯水沉淀已完成')
            actual=num(d.get('actual_dry_yield_g'),'实际干粉质量 g',zero=True)
            record.update(wash_cycles_count=int(cycles),wash_water_ml=water,wash_ethanol_ml=ethanol,actual_dry_yield_g=actual,
                          precipitant_medium='DI_WATER_HIGH_SPEED',wash_solvent_formula='EtOH/H2O',precipitation_confirmed=True)
    return record
