"""Collect group samples; configure bounded daily gambling; inspect/export results."""
import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/src'))
from tg_game.features.market import stone_gambling as stone

DEFAULT_DB=Path(__file__).resolve().parents[1]/'data/tg_game.db'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=DEFAULT_DB)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('collect')
    sub.add_parser('watch')
    stats=sub.add_parser('stats'); stats.add_argument('--own',action='store_true',help='only the three local accounts')
    export=sub.add_parser('export'); export.add_argument('path',type=Path)
    cfg=sub.add_parser('enable')
    cfg.add_argument('--profile',required=True,type=int)
    cfg.add_argument('--chat',required=True,type=int)
    cfg.add_argument('--thread',type=int)
    cfg.add_argument('--time',required=True)
    cfg.add_argument('--max-price',type=int,default=5000)
    cfg.add_argument('--budget',type=int,default=5500)
    cfg.add_argument('--pick',choices=list(stone.PICKS),help='which price rank to cut (default: keep the current one, cheapest when new)')
    disable=sub.add_parser('disable'); disable.add_argument('--profile',type=int)
    review=sub.add_parser('review'); review.add_argument('--run',type=int,required=True)
    sub.add_parser('status')
    args=parser.parse_args()
    db=stone.connect(args.database)
    stone.ensure_schema(db)
    if args.action=='enable':
        try: datetime.strptime(args.time,'%H:%M')
        except ValueError: parser.error('--time must be HH:MM in Beijing time')
        if not 0<args.max_price<=100000 or not stone.ENTRY_FEE<args.budget<=100500:
            parser.error('invalid price or daily budget')
        profile=db.execute('SELECT * FROM profiles WHERE id=?',(args.profile,)).fetchone()
        if not profile or not profile['telegram_verified_at']:
            parser.error('profile must be active and logged in')
        # Enable begins today, but only the scheduled 15-minute window can trigger a run.
        day=datetime.now(stone.TZ).date().isoformat()
        with db:
            db.execute('''INSERT INTO stone_auto_config(profile_id,chat_id,thread_id,local_time,enabled,max_price,daily_budget,start_day,pick)
                VALUES(?,?,?,?,1,?,?,?,coalesce(?,'cheapest'))
                ON CONFLICT(profile_id) DO UPDATE SET chat_id=excluded.chat_id,thread_id=excluded.thread_id,
                local_time=excluded.local_time,enabled=1,max_price=excluded.max_price,daily_budget=excluded.daily_budget,start_day=excluded.start_day,
                pick=coalesce(?,stone_auto_config.pick)''',
                (args.profile,args.chat,args.thread,args.time,args.max_price,args.budget,day,args.pick,args.pick))
            pick=db.execute('SELECT pick FROM stone_auto_config WHERE profile_id=?',(args.profile,)).fetchone()[0]
        print(f'Enabled profile {args.profile}: {args.time} Beijing, once/day, pick {pick}, price <= {args.max_price}, budget {args.budget}.')
    elif args.action=='review':
        with db:
            count=db.execute("UPDATE stone_auto_runs SET state='reviewed' WHERE id=? AND state='stopped'",(args.run,)).rowcount
        print(f'Reviewed {count} stopped run(s); re-enable separately when ready.')
    elif args.action=='disable':
        with db:
            where=' WHERE profile_id=?' if args.profile else ''
            params=(args.profile,) if args.profile else ()
            db.execute('UPDATE stone_auto_config SET enabled=0'+where,params)
            for r in db.execute("SELECT id FROM stone_auto_runs WHERE state IN ('entry_pending','await_panel','cut_pending','await_result')"+(' AND profile_id=?' if args.profile else ''),params).fetchall():
                stone.stop_run(db,r[0],'用户关闭自动赌石',time.time())
        print('Disabled. Sample collection continues.')
    elif args.action in ('collect','watch'):
        logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
        beat=0.0
        while True:
            now=time.time()
            try:
                beat=stone.watch_pass(db,now,args.action=='watch',beat)
                if args.action=='collect':break
            except Exception:
                logging.exception('Stone collector failed; no automatic retry of game commands')
                if args.action=='collect':raise
            time.sleep(2)
    elif args.action=='status':
        for table in ('stone_auto_config','stone_auto_runs'):
            rows=db.execute(f'SELECT * FROM {table} ORDER BY '+('id DESC LIMIT 30' if table.endswith('runs') else 'profile_id')).fetchall()
            print(table,json.dumps([dict(r) for r in rows],ensure_ascii=False))
        hb=db.execute("SELECT value FROM stone_meta WHERE key='worker_heartbeat'").fetchone()
        print('worker_heartbeat', hb[0] if hb else 'never')
    elif args.action=='stats':
        scope='sender_id IN (SELECT telegram_user_id FROM profiles)' if args.own else '1=1'
        print('Own accounts' if args.own else 'All observed players')
        print('Rounds / selected / confirmed results:',tuple(db.execute(f"SELECT count(*),count(cut_id),sum(link_status='matched') FROM stone_rounds WHERE {scope}").fetchone()))
        for r in db.execute(f"SELECT outcome,count(*) FROM stone_rounds WHERE link_status='matched' AND {scope} GROUP BY outcome"):
            print(r[0],r[1])
        print('Selected descriptions (observational, players choose their own stones):')
        groups={}
        for r in db.execute(f"SELECT * FROM stone_rounds WHERE link_status='matched' AND {scope}"):
            s=next(s for s in json.loads(r['stones_json']) if s['number']==r['selected'])
            key=s['description']; g=groups.setdefault(key,{'n':0,'empty':0,'items':{}})
            g['n']+=1;g['empty']+=r['outcome']=='垮了'
            for x in json.loads(r['reward_json']):g['items'][x['name']]=g['items'].get(x['name'],0)+x['quantity']
        for desc,g in sorted(groups.items(),key=lambda x:-x[1]['n']):
            print(json.dumps({'description':desc,**g},ensure_ascii=False))
    elif args.action=='export':
        rows=db.execute('SELECT * FROM stone_rounds ORDER BY created_at').fetchall()
        with args.path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=[x[1] for x in db.execute('PRAGMA table_info(stone_rounds)')]);writer.writeheader()
            writer.writerows(dict(r) for r in rows)
        print(f'Exported {len(rows)} rounds to {args.path}')
    db.close()


if __name__=='__main__':main()
