#!/usr/bin/env python3
"""Calibrate and apply an A-from-C multi-start placement surrogate.

Fit input is a TSV with case_id, original_log, placement_log and an optional
comparison_summary.  C log metrics are the only predictor inputs.  Matched STA
from comparison_summary, when supplied, replaces logged A WNS/TNS targets.
"""
import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

from CompareTimingPlacement import read_run

METRICS=('wns','tns','hpwl','overflow','max_density','congestion_max','congestion_total',
         'macro_congestion_max','macro_congestion_total','iteration')
HIGHER_BETTER={'wns','tns'}


def ranks(a):
    order=np.argsort(a,kind='mergesort');r=np.empty(len(a),float);i=0
    while i<len(a):
        j=i+1
        while j<len(a) and a[order[j]]==a[order[i]]:j+=1
        r[order[i:j]]=(i+j-1)/2;i=j
    return r


def corr(a,b):
    if len(a)<2 or np.std(a)==0 or np.std(b)==0:return None
    return float(np.corrcoef(a,b)[0,1])


def fit_line(x,y):
    if len(x)<2: raise ValueError('Cannot fit an affine model with fewer than two samples')
    if np.var(x)==0:return float(y.mean()),0.0
    slope=float(np.sum((x-x.mean())*(y-y.mean()))/np.sum((x-x.mean())**2))
    return float(y.mean()-slope*x.mean()),slope


def log_metrics(path):
    report,_=read_run(path,None); out={}
    for name,row in report['metrics'].items():
        if row['value'] is not None:out[name]=float(row['value'])
    return out,report


def load_pairs(path):
    rows=[]
    with open(path,encoding='utf-8-sig',newline='') as f:
        rd=csv.DictReader(f,delimiter='\t')
        required={'case_id','original_log','placement_log'}
        if not rd.fieldnames or not required<=set(rd.fieldnames):raise ValueError('pairs TSV needs '+','.join(sorted(required)))
        for row in rd:
            a,ar=log_metrics(row['original_log']);c,cr=log_metrics(row['placement_log'])
            summary=row.get('comparison_summary','').strip()
            if summary:
                data=json.loads(Path(summary).read_text())
                if not data.get('sta'):raise ValueError('comparison_summary has no matched STA: '+summary)
                sta=data['sta']['cases']['A_original']
                a['wns']=float(sta['wns_ps_late']);a['tns']=float(sta['tns_ps_late'])
            rows.append(dict(case_id=row['case_id'],a=a,c=c,original_log=row['original_log'],
                             placement_log=row['placement_log'],comparison_summary=summary,
                             original_seed=ar['parameters'].get('random_seed'),
                             placement_seed=cr['parameters'].get('random_seed')))
    if len(rows)<3:raise ValueError('At least three paired cases are required')
    if len({r['case_id'] for r in rows})!=len(rows):raise ValueError('Duplicate case_id')
    return rows


def fit(args):
    rows=load_pairs(args.pairs);models={};audits=[]
    for metric in METRICS:
        selected=[r for r in rows if metric in r['a'] and metric in r['c']]
        if len(selected)<3:continue
        x=np.array([r['c'][metric] for r in selected],float);y=np.array([r['a'][metric] for r in selected],float)
        if np.var(x)==0:continue
        intercept,slope=fit_line(x,y);pred=intercept+slope*x
        cv=np.empty(len(x))
        for i in range(len(x)):
            mask=np.arange(len(x))!=i;b0,b1=fit_line(x[mask],y[mask]);cv[i]=b0+b1*x[i]
        k=min(args.top_k,len(x));reverse=metric in HIGHER_BETTER
        true_order=np.argsort(y);pred_order=np.argsort(cv)
        if reverse:true_order=true_order[::-1];pred_order=pred_order[::-1]
        overlap=len(set(true_order[:k])&set(pred_order[:k]))/k
        models[metric]=dict(feature='c_'+metric,intercept=intercept,slope=slope,count=len(x),
            train_pearson=corr(x,y),train_spearman=corr(ranks(x),ranks(y)),
            loocv_mae=float(np.mean(np.abs(cv-y))),loocv_rmse=float(np.sqrt(np.mean((cv-y)**2))),
            loocv_pearson=corr(cv,y),loocv_spearman=corr(ranks(cv),ranks(y)),top_k=k,
            loocv_top_k_overlap=overlap,higher_is_better=reverse)
        for r,xx,yy,pp in zip(selected,x,y,cv):audits.append((r['case_id'],metric,xx,yy,pp,pp-yy))
    if not models:raise ValueError('No common nonconstant A/C metrics to fit')
    out=Path(args.output)
    if out.exists():raise FileExistsError('Choose a new output directory: '+str(out))
    out.mkdir(parents=True)
    model=dict(schema=1,kind='independent_affine_A_from_C',pairs=str(Path(args.pairs).resolve()),
               cases=len(rows),models=models)
    (out/'model.json').write_text(json.dumps(model,indent=2,allow_nan=False)+'\n')
    with (out/'cross_validation.tsv').open('w',newline='') as f:
        w=csv.writer(f,delimiter='\t');w.writerow(['case_id','metric','C_value','A_value','A_predicted_LOOCV','error']);w.writerows(audits)
    lines=['# A-from-C placement surrogate','',f'Paired cases: {len(rows)}','',
           '| Metric | N | slope | intercept | Spearman C/A | LOOCV Spearman | LOOCV MAE | Top-K overlap |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,m in models.items():
        fmt=lambda v:'NA' if v is None else '%.6g'%v
        lines.append('| %s | %d | %s | %s | %s | %s | %s | %.3f |'%(name,m['count'],fmt(m['slope']),
            fmt(m['intercept']),fmt(m['train_spearman']),fmt(m['loocv_spearman']),fmt(m['loocv_mae']),m['loocv_top_k_overlap']))
    lines += ['','Predictions use only final C log metrics. Matched A STA is used only as a calibration target.','']
    (out/'report.md').write_text('\n'.join(lines))
    return model


def predict(args):
    model=json.loads(Path(args.model).read_text());c,_=log_metrics(args.placement_log);result={}
    for metric,m in model['models'].items():
        if metric in c:result[metric]=m['intercept']+m['slope']*c[metric]
    if not result:raise ValueError('C log has none of the model features')
    payload=dict(schema=1,case_id=args.case_id,model=str(Path(args.model).resolve()),
                 placement_log=str(Path(args.placement_log).resolve()),C_metrics=c,predicted_A_metrics=result)
    Path(args.output).write_text(json.dumps(payload,indent=2,allow_nan=False)+'\n')
    return payload


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('fit');q.add_argument('--pairs',required=True);q.add_argument('--output',required=True);q.add_argument('--top-k',type=int,default=3)
    q=sub.add_parser('predict');q.add_argument('--model',required=True);q.add_argument('--placement-log',required=True);q.add_argument('--case-id',required=True);q.add_argument('--output',required=True)
    return p.parse_args(argv)


if __name__=='__main__':
    a=parse_args()
    if a.command=='fit':fit(a);print('Saved surrogate:',a.output)
    else:print(json.dumps(predict(a)['predicted_A_metrics'],indent=2))
