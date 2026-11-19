import csv, io
from app.errors import BusinessError
from app.reasons import reject

def read_source(state, fields, aliases):
    raw=state['raw_bytes']
    if not raw: raise BusinessError('EMPTY_FILE','请选择非空文件')
    if not raw.strip(b'\xef\xbb\xbf\r\n \t'): raise BusinessError('BLANK_FILE','文件只有空行')
    text=None
    for encoding in ('utf-8-sig','gb18030'):
        try: text=raw.decode(encoding);break
        except UnicodeDecodeError: continue
    if text is None: raise BusinessError('BAD_ENCODING','文件编码无法识别')
    stream=csv.reader(io.StringIO(text,newline=''),strict=True)
    try:
        original=next(stream)
        header=[aliases.get(v.strip(),v.strip()) for v in original]
        if header!=fields: raise BusinessError('BAD_HEADER','字段或顺序不匹配')
        state['counts']={'input':0,'blank':0,'repeated_header':0,'duplicate':0}
        records=[]
        for cells in stream:
            line=stream.line_num
            state['counts']['input']+=1
            if not cells or not any(v.strip() for v in cells):
                state['counts']['blank']+=1;continue
            if cells==original:
                state['counts']['repeated_header']+=1;continue
            row=dict(zip(header,cells));row['_line']=line
            if len(cells)!=len(header): reject(state,row,'COLUMN_COUNT',f'需要{len(header)}列，实际{len(cells)}列');continue
            if not row.get('id','').strip(): reject(state,row,'MISSING_ID','编号不能为空');continue
            row['id']=row['id'].strip();records.append(row)
    except csv.Error:
        raise BusinessError('BROKEN_CSV','CSV引号或末行损坏')
    if not state['counts']['input']: raise BusinessError('HEADER_ONLY','文件只有表头')
    state['records'],state['attribution']=attribute(state,records)
    state['encoding']=encoding
    rows=state['attribution']
    metrics={k:sum(1 for r in rows if r['decision']==k) for k in ('接受','替代','待核')}
    state['views'][state['context']['keys'][0]]={'rows':rows,'metrics':metrics,'title':'事件归属'}

def attribute(state, records):
    """同一事件编号多来源冲突：先到行暂行采信，后到行隔离待核，复核指定行替代。
    归属决定来自 context['attribution']（事件号→原始行号），随上下文进入批次摘要，
    同输入与决定重放结果不变；决定只生成新批次，不改写旧批次的原依据。"""
    decisions=state['context'].get('attribution',{})
    groups={}
    for row in records: groups.setdefault(row['id'],[]).append(row)
    conflicts=set()
    for key,rows in groups.items():
        rows.sort(key=lambda r:r['_line'])
        identities={tuple((k,v) for k,v in r.items() if k!='_line') for r in rows}
        if len(identities)>1: conflicts.add(key)
    unknown=sorted(set(decisions)-conflicts)
    if unknown: raise BusinessError('ATTRIBUTION_INVALID',f'事件{",".join(unknown)}无编号冲突，归属决定不适用')
    selected=[];attribution=[]
    for key,rows in groups.items():
        if key not in conflicts:
            chosen=rows[0];selected.append(chosen);state['counts']['duplicate']+=len(rows)-1
            attribution.append({'id':key,'decision':'接受','line':chosen['_line'],'basis':'唯一记录' if len(rows)==1 else '同值去重','publish':'已发布'})
            continue
        lines=[r['_line'] for r in rows]
        if key in decisions:
            line=decisions[key]
            if line not in lines: raise BusinessError('ATTRIBUTION_INVALID',f'事件{key}的归属行{line}不在冲突原始行{lines}中')
            replaced=[n for n in lines if n<line]
            selected.append(next(r for r in rows if r['_line']==line))
            basis='复核指定·替代行'+','.join(map(str,replaced)) if replaced else '复核指定'
            attribution.append({'id':key,'decision':'接受','line':line,'basis':basis,'publish':'已发布'})
            for row in rows:
                if row['_line']==line: continue
                if row['_line']<line:
                    reject(state,row,'SUPERSEDED',f'归属复核后由行{line}替代')
                    attribution.append({'id':key,'decision':'替代','line':row['_line'],'basis':f'复核替代·归属行{line}','publish':'已替代'})
                else:
                    reject(state,row,'DUPLICATE_CONFLICT',f'同一编号存在不同原值，当前归属行{line}')
                    attribution.append({'id':key,'decision':'待核','line':row['_line'],'basis':f'编号冲突·当前归属行{line}','publish':'待核'})
        else:
            chosen=rows[0];selected.append(chosen)
            attribution.append({'id':key,'decision':'接受','line':chosen['_line'],'basis':'先到采信·待核行'+','.join(map(str,lines[1:])),'publish':'暂行发布'})
            for row in rows[1:]:
                reject(state,row,'DUPLICATE_CONFLICT',f'同一编号存在不同原值，当前归属行{chosen["_line"]}')
                attribution.append({'id':key,'decision':'待核','line':row['_line'],'basis':f'编号冲突·当前归属行{chosen["_line"]}','publish':'待核'})
    return selected,attribution
