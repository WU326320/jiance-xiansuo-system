import json,re,collections
out={}
for p in ['taobao','jd','1688']:
    rs=[json.loads(l) for l in open(f'out/2026-11-05/{p}.jsonl',encoding='utf-8') if l.strip()]
    n=len(rs); nn=lambda k: sum(1 for r in rs if r.get(k) not in (None,''))
    fz=[r for r in rs for k in ('sales','comments') if r.get(k+'_raw') and re.search(r'[+万]',str(r.get(k+'_raw')))]
    fz_ok=sum(1 for r in rs for k in ('sales','comments') if r.get(k+'_raw') and re.search(r'[+万]',str(r.get(k+'_raw'))) and r.get(k) is not None)
    title_hit=sum(1 for r in rs if '奥特曼' in (r.get('title') or ''))
    flags=collections.Counter(f for r in rs for f in r.get('flags',[]))
    bad_flag=sum(1 for r in rs if '店铺ID缺失' in r.get('flags',[]) and r.get('shop_id'))
    out[p]=dict(n=n,shop_id=nn('shop_id'),sales_raw=nn('sales_raw'),comments_raw=nn('comments_raw'),shop_type=nn('shop_type_raw_hint'),price=nn('price'),title=nn('title'),image=nn('main_image_url'),title_hit=title_hit,fuzzy=len(fz),fuzzy_ok=fz_ok,flag_conflict=bad_flag,flags=dict(flags.most_common(8)),
      samples=[{k:r.get(k) for k in ('product_id','title','shop_id','shop_name','shop_type_raw_hint','price_raw','sales_raw','sales','comments_raw','comments','flags')} for r in rs[:3]])
json.dump(out,open('trial_metrics.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
for p,v in out.items(): print(p,{k:v[k] for k in v if k not in('samples',)})
