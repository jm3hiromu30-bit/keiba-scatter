#!/usr/bin/env python3
"""
競馬クッション値×含水率 散布図 一括生成パイプライン

使い方:
  python pipeline.py 20260215              # 全36レース生成
  python pipeline.py 20260215 --venue 東京  # 東京のみ
  python pipeline.py 20260215 --race 11    # 全場の11Rのみ
  python pipeline.py 20260215 --deploy     # 生成後にGitHub Pagesへ自動デプロイ
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import sys
import argparse
import base64
from datetime import datetime
from urllib.parse import quote

# ===== 設定 =====
CUSHION_DB_PATH = os.path.join(os.path.dirname(__file__), 'cushion_db_full.json')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')

VENUE_CODES = {
    '01': '札幌', '02': '函館', '03': '福島', '04': '新潟',
    '05': '東京', '06': '中山', '07': '中京', '08': '京都',
    '09': '阪神', '10': '小倉'
}


# ===== Step 1: JRA ライブデータ取得 =====
def fetch_jra_live():
    """JRA公式からクッション値・含水率をリアルタイム取得"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    result = {}

    # クッション値
    r = requests.get('https://www.jra.go.jp/keiba/baba/_data_cushion.html', headers=headers)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
        venue = div.get('title', '')
        units = div.find_all('div', class_='unit')
        if units:
            cushion_text = units[0].find('div', class_='cushion').get_text(strip=True)
            time_text = units[0].find('div', class_='time').get_text(strip=True)
            result[venue] = {'cushion': float(cushion_text), 'time_cushion': time_text}

    # 含水率
    r = requests.get('https://www.jra.go.jp/keiba/baba/_data_moist.html', headers=headers)
    r.encoding = 'shift_jis'
    soup = BeautifulSoup(r.text, 'html.parser')
    for div in soup.find_all('div', id=re.compile(r'^rc[A-Z]')):
        venue = div.get('title', '')
        units = div.find_all('div', class_='unit')
        if units:
            u = units[0]
            turf_div = u.find('div', class_='turf')
            dirt_div = u.find('div', class_='dirt')
            turf_mg = float(turf_div.find('span', class_='mg').get_text(strip=True)) if turf_div else None
            dirt_mg = float(dirt_div.find('span', class_='mg').get_text(strip=True)) if dirt_div else None
            time_text = u.find('div', class_='time').get_text(strip=True)
            if venue in result:
                result[venue]['turf_moisture'] = turf_mg
                result[venue]['dirt_moisture'] = dirt_mg
                result[venue]['time_moisture'] = time_text
            else:
                result[venue] = {'turf_moisture': turf_mg, 'dirt_moisture': dirt_mg, 'time_moisture': time_text}

    return result


# ===== Step 2: レース一覧取得 =====
def get_race_list(date_str):
    """netkeiba からレース一覧取得 (date_str: YYYYMMDD)"""
    url = f'https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    r = requests.get(url, headers=headers)
    r.encoding = 'utf-8'
    soup = BeautifulSoup(r.text, 'html.parser')

    links = soup.find_all('a', href=re.compile(r'race_id=\d+'))
    seen = set()
    races = []
    for link in links:
        m = re.search(r'race_id=(\d+)', link.get('href', ''))
        if m and m.group(1) not in seen:
            rid = m.group(1)
            seen.add(rid)
            text = link.get_text(strip=True)
            venue_code = rid[4:6]
            venue = VENUE_CODES.get(venue_code, '?')
            race_num = int(rid[10:12])

            # Parse surface and distance from text
            sd_match = re.search(r'(芝|ダ|障)(\d+)m', text)
            surface = sd_match.group(1) if sd_match else '?'
            distance = int(sd_match.group(2)) if sd_match else 0

            # Parse race name
            name_match = re.match(r'\d+R(.+?)[\d:]+', text)
            race_name = name_match.group(1) if name_match else text

            races.append({
                'race_id': rid,
                'venue': venue,
                'race_num': race_num,
                'race_name': race_name,
                'surface': surface,
                'distance': distance,
                'text': text,
            })

    return races


# ===== Step 3: 出走馬+過去戦績取得 =====
def scrape_race_data(race_id):
    """netkeiba から出走馬と各馬の過去戦績を取得"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })

    # 出馬表取得
    url = f'https://race.netkeiba.com/race/shutuba.html?race_id={race_id}'
    r = session.get(url)
    r.encoding = 'euc-jp'
    soup = BeautifulSoup(r.text, 'html.parser')

    race_name_tag = soup.find('div', class_='RaceName')
    race_name = race_name_tag.get_text(strip=True) if race_name_tag else ''
    race_data_tag = soup.find('div', class_='RaceData01')
    race_data_text = race_data_tag.get_text(strip=True) if race_data_tag else ''
    sd_match = re.search(r'(芝|ダ|障)(\d+)m', race_data_text)
    surface = sd_match.group(1) if sd_match else '?'
    distance = int(sd_match.group(2)) if sd_match else 0
    venue_code = race_id[4:6]
    venue = VENUE_CODES.get(venue_code, '?')

    # 馬一覧
    horses = []
    table = soup.find('table', class_='Shutuba_Table') or soup.find('table', id='shutuba_table')
    if not table:
        print(f"    WARNING: Shutuba table not found")
        return None

    rows = table.find_all('tr', class_='HorseList')
    for row in rows:
        horse_link = row.find('a', href=re.compile(r'/horse/\d+'))
        if not horse_link:
            continue
        horse_name = horse_link.get_text(strip=True)
        horse_id_match = re.search(r'/horse/(\d+)', horse_link.get('href', ''))
        horse_id = horse_id_match.group(1) if horse_id_match else None
        horses.append({'name': horse_name, 'horse_id': horse_id})

    # 各馬の過去戦績
    all_horses = {}
    for h in horses:
        results = get_horse_results(session, h['horse_id'])
        all_horses[h['name']] = results
        print(f"    {h['name']}: {len(results)}走")
        time.sleep(0.5)

    return {
        'race_info': {
            'race_id': race_id,
            'race_name': race_name,
            'venue': venue,
            'surface': surface,
            'distance': distance,
        },
        'horses': all_horses,
    }


def get_horse_results(session, horse_id, max_races=10):
    """馬の過去戦績を取得"""
    url = f'https://db.netkeiba.com/horse/result/{horse_id}/'
    r = session.get(url)
    r.encoding = 'euc-jp'
    soup = BeautifulSoup(r.text, 'html.parser')

    results = []
    table = soup.find('table', class_='db_h_race_results')
    if not table:
        return results

    venue_short_map = {
        '東': '東京', '京': '京都', '中': '中山', '阪': '阪神',
        '小': '小倉', '新': '新潟', '福': '福島', '函': '函館',
        '札': '札幌', '中京': '中京',
    }

    rows = table.find_all('tr')
    for tr in rows[1:max_races + 1]:
        cells = tr.find_all('td')
        if len(cells) < 15:
            continue
        try:
            date = cells[0].get_text(strip=True)
            venue_raw = cells[1].get_text(strip=True)
            race_name = cells[4].get_text(strip=True)
            result_text = cells[11].get_text(strip=True)
            result = int(result_text) if result_text.isdigit() else None
            dist_text = cells[14].get_text(strip=True)
            sd_match = re.search(r'(芝|ダ|障)(\d+)', dist_text)
            surface = sd_match.group(1) if sd_match else '?'
            distance = int(sd_match.group(2)) if sd_match else 0
            venue = re.sub(r'\d+', '', venue_raw).strip()
            for short, full in venue_short_map.items():
                if venue == short:
                    venue = full
                    break

            results.append({
                'date': date,
                'venue': venue,
                'surface': surface,
                'distance': distance,
                'race_name': race_name,
                'result': result,
            })
        except Exception:
            continue

    return results


# ===== Step 4: クッション値紐付け =====
def link_cushion_data(race_data, cushion_db):
    """各馬の過去レースにクッション値・含水率を紐付け"""
    for horse_name, races in race_data['horses'].items():
        for r in races:
            date = r['date']
            venue = r['venue']
            surface = r.get('surface', '芝')

            key = f"{date}_{venue}"
            if key in cushion_db:
                entry = cushion_db[key]
                r['cushion'] = entry['cushion']
                if surface == 'ダ' or surface == 'ダート':
                    r['moisture'] = entry.get('dirt_goal')
                else:
                    r['moisture'] = entry.get('turf_goal')
            else:
                r['cushion'] = None
                r['moisture'] = None

    return race_data


# ===== Step 5: 散布図HTML生成 =====
def generate_scatter_html(race_data, target_cushion, target_moisture, output_path, date_label='', race_num=0):
    """散布図HTMLを生成"""
    race_info = race_data['race_info']
    venue = race_info['venue']
    race_name = race_info['race_name']
    surface = race_info['surface']
    distance = race_info['distance']

    js_horses = []
    for horse_name, races in race_data['horses'].items():
        js_races = []
        for r in races:
            if r.get('cushion') is None or r.get('moisture') is None:
                continue
            if r['surface'] != surface:
                cat = 'diff_surface'
            elif r['distance'] == distance:
                cat = 'same_dist'
            else:
                cat = 'diff_dist'

            js_races.append({
                'date': r['date'],
                'venue': r['venue'],
                'surface': r['surface'],
                'distance': r['distance'],
                'race_name': r['race_name'],
                'result': r['result'],
                'cushion': r['cushion'],
                'moisture': r['moisture'],
                'cat': cat,
                'good': r['result'] is not None and r['result'] <= 3,
            })
        js_horses.append({'name': horse_name, 'races': js_races})

    horses_json = json.dumps(js_horses, ensure_ascii=False)
    surface_label = '芝' if surface == '芝' else 'ダート'
    color_same = f'同距離{surface_label}'
    color_diff = f'他距離{surface_label}'
    color_other = 'ダート' if surface == '芝' else '芝レース'

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>クッション値×含水率 - {venue}{race_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Noto Sans JP', sans-serif;
  background: #fff; color: #1e293b; overflow: hidden; height: 100vh;
}}
.header {{
  background: #fff; border-bottom: 1px solid #e2e8f0;
  padding: 12px 16px; z-index: 100;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05); flex-shrink: 0;
}}
.header h1 {{ font-size: 16px; font-weight: 900; letter-spacing: -0.5px; }}
.header .sub {{ font-size: 11px; color: #64748b; margin-top: 2px; }}
.header .target {{
  display: inline-flex; gap: 12px; margin-top: 4px;
  font-size: 11px; font-weight: 700; font-family: monospace;
}}
.header .target span {{
  background: #f8fafc; border: 1px solid #e2e8f0;
  padding: 2px 8px; border-radius: 4px;
}}
.main {{ display: flex; flex-direction: column; flex: 1; overflow: hidden; }}
@media (min-width: 768px) {{ .main {{ flex-direction: row; }} }}
.chart-area {{ position: relative; width: 100%; height: 40vh; min-height: 250px; flex-shrink: 0; }}
@media (min-width: 768px) {{ .chart-area {{ flex: 1; height: 100%; }} }}
canvas {{ display: block; width: 100% !important; height: 100% !important; touch-action: pan-y; }}
.panel {{
  border-top: 1px solid #e2e8f0; overflow-y: auto; padding: 8px 8px 80px 8px; background: #f8fafc;
  flex: 1;
}}
@media (min-width: 768px) {{
  .panel {{ width: 320px; border-top: none; border-left: 1px solid #e2e8f0; }}
}}
.horse-btn {{
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 10px 14px; margin-bottom: 4px; border: 1px solid #e2e8f0;
  border-radius: 12px; background: #fff; cursor: pointer;
  transition: all 0.2s; font-size: 14px; font-weight: 700; color: #1e293b;
  -webkit-tap-highlight-color: transparent;
}}
.horse-btn:active {{ transform: scale(0.98); }}
.horse-btn.selected {{
  border-color: #f59e0b; background: #fffbeb;
  box-shadow: 0 0 0 2px rgba(245,158,11,0.2);
}}
.horse-btn .count {{ font-size: 10px; color: #94a3b8; font-weight: 600; margin-left: auto; }}
.horse-btn .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.rating-row {{
  display: flex; gap: 4px; padding: 4px 14px 8px 22px;
}}
.rating-btn {{
  width: 32px; height: 28px; border: 1.5px solid #cbd5e1; border-radius: 6px;
  background: #fff; cursor: pointer; font-size: 12px; font-weight: 800;
  color: #94a3b8; transition: all 0.15s; -webkit-tap-highlight-color: transparent;
}}
.rating-btn:active {{ transform: scale(0.92); }}
.rating-btn.rated-S {{ background: #dc2626; border-color: #dc2626; color: #fff; }}
.rating-btn.rated-A {{ background: #f59e0b; border-color: #f59e0b; color: #fff; }}
.rating-btn.rated-B {{ background: #3b82f6; border-color: #3b82f6; color: #fff; }}
.rating-btn.rated-C {{ background: #22c55e; border-color: #22c55e; color: #fff; }}
.rating-btn.rated-D {{ background: #94a3b8; border-color: #94a3b8; color: #fff; }}
.horse-detail {{ display: none; padding: 8px 4px; }}
.horse-detail.show {{ display: block; }}
.race-card {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 6px; }}
.race-item {{
  padding: 8px 10px; border-radius: 10px; border: 1px solid #e2e8f0;
  background: #fff; font-size: 10px; cursor: pointer;
}}
.race-item.ideal {{ background: #ecfdf5; border-color: #a7f3d0; }}
.race-item.highlighted {{ border-color: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,0.3); }}
.race-item .date {{ color: #94a3b8; font-weight: 600; font-family: monospace; }}
.race-item .rname {{ color: #1e293b; font-weight: 700; font-size: 10px; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.race-item .result {{ font-size: 13px; font-weight: 900; }}
.race-item .cond {{ color: #64748b; font-weight: 700; }}
.legend {{
  display: flex; gap: 12px; padding: 8px 16px; font-size: 10px;
  font-weight: 700; color: #94a3b8; border-top: 1px solid #e2e8f0; flex-wrap: wrap;
}}
.legend span {{ display: flex; align-items: center; gap: 4px; }}
.legend .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
.tooltip {{
  display: none; position: fixed; background: rgba(15,23,42,0.95); color: #fff;
  padding: 10px 14px; border-radius: 10px; font-size: 12px; line-height: 1.6;
  pointer-events: none; z-index: 200; max-width: 250px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}}
.tooltip.show {{ display: block; }}
</style>
</head>
<body style="display:flex;flex-direction:column;">
<div class="header">
  <h1>{venue}{race_num}R {race_name} {surface}{distance}m</h1>
  <div class="sub">出走馬 クッション値×含水率 解析</div>
  <div class="target">
    <span>CV: <b style="color:#d97706">{target_cushion}</b></span>
    <span>含水率: <b style="color:#2563eb">{target_moisture}%</b></span>
    <span style="color:#94a3b8">{date_label} {venue}</span>
  </div>
</div>
<div class="main">
  <div class="chart-area"><canvas id="chart"></canvas><div class="tooltip" id="tooltip"></div></div>
  <div class="panel" id="panel"></div>
</div>
<div class="legend">
  <span><span class="dot" style="background:#dc2626"></span> {color_same}</span>
  <span><span class="dot" style="background:#2563eb"></span> {color_diff}</span>
  <span><span class="dot" style="background:#94a3b8"></span> {color_other}</span>
  <span>○ 3着以内 / × 4着以下</span>
</div>
<script>
const HORSES = {horses_json};
const TX = {target_cushion};
const TY = {target_moisture};
const TDIST = {distance};
const COLORS = {{ same_dist:'#dc2626', diff_dist:'#2563eb', diff_surface:'#94a3b8', target:'#d97706' }};
const X_MIN = 7.0, X_MAX = 12.0;
const Y_MIN = 0, Y_MAX = 22;
let selectedHorses = new Set();
let highlightedPoints = new Set();
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');
const tooltipEl = document.getElementById('tooltip');
const hueStep = 360 / Math.max(HORSES.length, 1);
const horseColors = HORSES.map((_, i) => `hsl(${{i * hueStep}}, 65%, 55%)`);

function resize() {{
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); draw();
}}
function toCanvasX(v) {{ const p=50; const w=canvas.width/(window.devicePixelRatio||1)-p*2; return p+(v-X_MIN)/(X_MAX-X_MIN)*w; }}
function toCanvasY(v) {{ const pt=20,pb=40; const h=canvas.height/(window.devicePixelRatio||1)-pt-pb; return pt+(1-(v-Y_MIN)/(Y_MAX-Y_MIN))*h; }}

function draw() {{
  const W=canvas.width/(window.devicePixelRatio||1), H=canvas.height/(window.devicePixelRatio||1);
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle='#f1f5f9'; ctx.lineWidth=1;
  for(let x=Math.ceil(X_MIN);x<=X_MAX;x+=0.5){{ const px=toCanvasX(x); ctx.beginPath();ctx.moveTo(px,20);ctx.lineTo(px,H-40);ctx.stroke(); ctx.fillStyle='#94a3b8';ctx.font='10px monospace';ctx.textAlign='center';ctx.fillText(x.toFixed(1),px,H-25); }}
  for(let y=0;y<=Y_MAX;y+=2){{ const py=toCanvasY(y); ctx.beginPath();ctx.moveTo(50,py);ctx.lineTo(W-50,py);ctx.stroke(); ctx.fillStyle='#94a3b8';ctx.font='10px monospace';ctx.textAlign='right';ctx.fillText(y+'%',45,py+4); }}
  ctx.fillStyle='#64748b';ctx.font='bold 11px sans-serif';ctx.textAlign='center';
  ctx.fillText('クッション値',W/2,H-5);
  ctx.save();ctx.translate(12,H/2);ctx.rotate(-Math.PI/2);ctx.fillText('含水率（ゴール前）%',0,0);ctx.restore();
  ctx.fillStyle='rgba(59,130,246,0.04)';
  let sx0=toCanvasX(TX-0.5),sx1=toCanvasX(TX+0.5),sy0=toCanvasY(TY+3),sy1=toCanvasY(TY-3);
  ctx.fillRect(sx0,sy0,sx1-sx0,sy1-sy0);
  ctx.fillStyle='rgba(16,185,129,0.1)';
  let ix0=toCanvasX(TX-0.2),ix1=toCanvasX(TX+0.2),iy0=toCanvasY(TY+1.5),iy1=toCanvasY(TY-1.5);
  ctx.fillRect(ix0,iy0,ix1-ix0,iy1-iy0);
  ctx.setLineDash([5,5]);ctx.strokeStyle='#e2e8f0';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(toCanvasX(TX),20);ctx.lineTo(toCanvasX(TX),H-40);ctx.stroke();
  ctx.beginPath();ctx.moveTo(50,toCanvasY(TY));ctx.lineTo(W-50,toCanvasY(TY));ctx.stroke();
  ctx.setLineDash([]);
  HORSES.forEach((h,hi)=>{{
    const isSel=selectedHorses.has(h.name), dimmed=selectedHorses.size>0&&!isSel;
    const alpha=dimmed?0.08:(isSel?1.0:0.7);
    h.races.forEach((r,ri)=>{{
      const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture),color=COLORS[r.cat];
      const isHL=highlightedPoints.has(hi+'-'+ri);
      const sz=isHL?18:(isSel?15:10);
      ctx.globalAlpha=isHL?1.0:alpha;
      if(isHL){{ctx.strokeStyle='#f59e0b';ctx.lineWidth=5;ctx.beginPath();ctx.arc(px,py,sz+6,0,Math.PI*2);ctx.stroke();}}
      if(r.good){{ ctx.beginPath();ctx.arc(px,py,sz,0,Math.PI*2);ctx.fillStyle=isHL?'#fffbeb':'#fff';ctx.fill();ctx.strokeStyle=isHL?'#f59e0b':color;ctx.lineWidth=isHL?4:(isSel?3.5:2);ctx.stroke(); }}
      else{{ ctx.strokeStyle=isHL?'#f59e0b':color;ctx.lineWidth=isHL?4:(isSel?3.5:2);ctx.beginPath();ctx.moveTo(px-sz,py-sz);ctx.lineTo(px+sz,py+sz);ctx.stroke();ctx.beginPath();ctx.moveTo(px+sz,py-sz);ctx.lineTo(px-sz,py+sz);ctx.stroke(); }}
      if(!dimmed||isHL){{ ctx.fillStyle=isHL?'#f59e0b':color;ctx.font=`bold ${{isHL?13:(isSel?11:8)}}px Arial`;ctx.textAlign='center';ctx.textBaseline='middle';ctx.strokeStyle='#fff';ctx.lineWidth=2.5;ctx.strokeText(r.result||'?',px,py+1);ctx.fillText(r.result||'?',px,py+1); }}
    }});
  }});
  ctx.globalAlpha=1;
  const RANK_COLORS={{S:'#dc2626',A:'#f59e0b',B:'#3b82f6',C:'#22c55e',D:'#94a3b8'}};
  HORSES.forEach((h,hi)=>{{
    if(ratings[h.name]){{
      const rank=ratings[h.name];const rc=RANK_COLORS[rank];
      h.races.forEach(r=>{{
        const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture);
        ctx.globalAlpha=selectedHorses.size>0&&!selectedHorses.has(h.name)?0.15:1;
        ctx.fillStyle=rc;ctx.font='bold 9px Arial';ctx.textAlign='left';
        ctx.fillText(rank,px+10,py-8);
      }});
    }}
  }});
  ctx.globalAlpha=1;
  const tx=toCanvasX(TX),ty=toCanvasY(TY);
  ctx.fillStyle=COLORS.target;ctx.font='bold 22px Arial';ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.strokeStyle='#fff';ctx.lineWidth=3;ctx.strokeText('★',tx,ty);ctx.fillText('★',tx,ty);
  ctx.textBaseline='alphabetic';
}}
const ratings = {{}};
function buildPanel(){{
  const panel=document.getElementById('panel');
  const RANKS=['S','A','B','C','D'];
  let html='';
  HORSES.forEach((h,i)=>{{
    const cnt=h.races.length;
    html+=`<button class="horse-btn" id="btn-${{i}}"><span class="dot" style="background:${{horseColors[i]}}"></span>${{h.name}}<span class="count">${{cnt>0?cnt+'走':'データなし'}}</span></button>`;
    html+=`<div class="rating-row" id="rate-${{i}}">`;
    RANKS.forEach(r=>{{html+=`<button class="rating-btn" data-horse="${{i}}" data-rank="${{r}}">${{r}}</button>`;}});
    html+=`</div>`;
    html+=`<div class="horse-detail" id="detail-${{i}}"><div class="race-card">${{h.races.map((r,ri)=>{{const inIdeal=Math.abs(r.cushion-TX)<=0.2&&Math.abs(r.moisture-TY)<=1.5;return`<div class="race-item ${{inIdeal?'ideal':''}}" data-horse="${{i}}" data-ri="${{ri}}"><div class="date">${{r.date}} ${{r.venue}}</div><div class="rname">${{r.race_name}}</div><div class="cond">${{r.surface}}${{r.distance}}m ${{r.distance===TDIST?'(同)':r.distance>TDIST?'(短)':'(延)'}}</div><div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px"><span style="font-size:9px;color:#94a3b8">CV${{r.cushion}} / ${{r.moisture}}%</span><span class="result" style="color:${{COLORS[r.cat]}}">${{r.result!==null?r.result+'着':'取消'}}</span></div></div>`}}).join('')}}</div></div>`;
  }});
  panel.innerHTML=html;
  HORSES.forEach((h,i)=>{{document.getElementById('btn-'+i).addEventListener('click',()=>{{
    const detail=document.getElementById('detail-'+i);
    if(selectedHorses.has(h.name)){{selectedHorses.delete(h.name);detail.classList.remove('show');document.getElementById('btn-'+i).classList.remove('selected');}}
    else{{selectedHorses.add(h.name);detail.classList.add('show');document.getElementById('btn-'+i).classList.add('selected');}}
    requestAnimationFrame(()=>{{draw();}});
  }});}});
  document.querySelectorAll('.rating-btn').forEach(btn=>{{
    btn.addEventListener('click',(e)=>{{
      e.stopPropagation();
      const hi=parseInt(btn.dataset.horse);
      const rank=btn.dataset.rank;
      const name=HORSES[hi].name;
      if(ratings[name]===rank){{delete ratings[name];}}
      else{{ratings[name]=rank;}}
      updateRatings();
    }});
  }});
  document.querySelectorAll('.race-item').forEach(el=>{{
    el.addEventListener('click',(e)=>{{
      e.stopPropagation();
      el.classList.toggle('highlighted');
      const key=el.dataset.horse+'-'+el.dataset.ri;
      if(highlightedPoints.has(key))highlightedPoints.delete(key);else highlightedPoints.add(key);
      requestAnimationFrame(()=>{{draw();}});
    }});
  }});
}}
function updateRatings(){{
  document.querySelectorAll('.rating-btn').forEach(btn=>{{
    const hi=parseInt(btn.dataset.horse);
    const rank=btn.dataset.rank;
    const name=HORSES[hi].name;
    btn.className='rating-btn'+(ratings[name]===rank?' rated-'+rank:'');
  }});
  draw();
}}
const isMobile='ontouchstart' in window;
function getPointAt(cx,cy){{
  let closest=null,minDist=isMobile?35:20;
  HORSES.forEach(h=>{{if(selectedHorses.size>0&&!selectedHorses.has(h.name))return;h.races.forEach(r=>{{const px=toCanvasX(r.cushion),py=toCanvasY(r.moisture),d=Math.sqrt((cx-px)**2+(cy-py)**2);if(d<minDist){{minDist=d;closest={{...r,horse:h.name}};}}}});}});
  return closest;
}}
canvas.addEventListener('mousemove',(e)=>{{const rect=canvas.getBoundingClientRect();const x=e.clientX-rect.left,y=e.clientY-rect.top;const pt=getPointAt(x,y);if(pt){{tooltipEl.innerHTML=`<b>${{pt.horse}}</b><br>${{pt.date}} ${{pt.venue}} ${{pt.surface}}${{pt.distance}}m<br>${{pt.race_name}}<br><b>${{pt.result}}着</b><br>CV: ${{pt.cushion}} / 含水率: ${{pt.moisture}}%`;tooltipEl.style.left=(e.clientX+15)+'px';tooltipEl.style.top=(e.clientY-10)+'px';tooltipEl.classList.add('show');}}else{{tooltipEl.classList.remove('show');}}}});
canvas.addEventListener('mouseleave',()=>tooltipEl.classList.remove('show'));
let touchTimer=null;
function showTooltipAt(cx,cy,tx,ty){{const pt=getPointAt(cx,cy);if(pt){{tooltipEl.innerHTML=`<b>${{pt.horse}}</b><br>${{pt.date}} ${{pt.venue}} ${{pt.surface}}${{pt.distance}}m<br>${{pt.race_name}}<br><b>${{pt.result!==null?pt.result+'着':'取消'}}</b><br>CV: ${{pt.cushion}} / 含水率: ${{pt.moisture}}%`;const left=Math.min(tx+15,window.innerWidth-260);const top=Math.max(ty-40,10);tooltipEl.style.left=left+'px';tooltipEl.style.top=top+'px';tooltipEl.classList.add('show');}}else{{tooltipEl.classList.remove('show');}}}}
canvas.addEventListener('touchstart',(e)=>{{const t=e.touches[0];const rect=canvas.getBoundingClientRect();showTooltipAt(t.clientX-rect.left,t.clientY-rect.top,t.clientX,t.clientY);}},{{passive:true}});
canvas.addEventListener('touchmove',(e)=>{{const t=e.touches[0];const rect=canvas.getBoundingClientRect();showTooltipAt(t.clientX-rect.left,t.clientY-rect.top,t.clientX,t.clientY);}},{{passive:true}});
canvas.addEventListener('touchend',()=>{{if(touchTimer)clearTimeout(touchTimer);touchTimer=setTimeout(()=>tooltipEl.classList.remove('show'),2000);}});
canvas.addEventListener('click',(e)=>{{const rect=canvas.getBoundingClientRect();showTooltipAt(e.clientX-rect.left,e.clientY-rect.top,e.clientX,e.clientY);}});
window.addEventListener('resize',resize); buildPanel(); resize();
</script>
</body></html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    total_pts = sum(len(h['races']) for h in js_horses)
    horses_with_data = sum(1 for h in js_horses if h['races'])
    return total_pts, horses_with_data, len(js_horses)


# ===== メインパイプライン =====
def main():
    parser = argparse.ArgumentParser(description='競馬クッション値×含水率 散布図 一括生成')
    parser.add_argument('date', help='開催日 (YYYYMMDD)')
    parser.add_argument('--venue', help='競馬場で絞り込み (東京/京都/小倉 等)')
    parser.add_argument('--race', type=int, help='レース番号で絞り込み (例: 11)')
    parser.add_argument('--no-scrape', action='store_true', help='キャッシュ済みデータのみ使用')
    parser.add_argument('--output', default=None, help='出力先ディレクトリ')
    parser.add_argument('--deploy', action='store_true', help='GitHub Pagesへ自動デプロイ')
    parser.add_argument('--manual', action='store_true', help='クッション値・含水率を会場別に手動入力')
    args = parser.parse_args()

    date_str = args.date
    date_label = f"{date_str[4:6]}/{date_str[6:8]}"
    out_dir = args.output or os.path.join(OUTPUT_DIR, date_str)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Step 1: レース一覧取得
    print("=" * 60)
    print(f"[Step 1] レース一覧取得 ({date_str})")
    print("=" * 60)
    races = get_race_list(date_str)

    # フィルタリング
    if args.venue:
        races = [r for r in races if r['venue'] == args.venue]
    if args.race:
        races = [r for r in races if r['race_num'] == args.race]

    # 障害レースを除外
    races = [r for r in races if r['surface'] != '障']

    print(f"  対象: {len(races)}レース")
    for r in races:
        print(f"    {r['venue']}{r['race_num']}R {r['race_name']} {r['surface']}{r['distance']}m")
    print()

    # Step 2: クッション値・含水率
    print("=" * 60)
    print(f"[Step 2] クッション値・含水率 取得")
    print("=" * 60)
    manual_mode = args.manual
    if manual_mode:
        # 会場別に手動入力
        venues_in_races = sorted(set(r['venue'] for r in races))
        jra_live = {}
        print(f"  *** 手動入力モード ({len(venues_in_races)}会場) ***")
        print()
        for v in venues_in_races:
            print(f"  [{v}]")
            cv = input(f"    クッション値 (例: 9.5): ")
            mt = input(f"    芝 含水率% (例: 12.0): ")
            md = input(f"    ダート 含水率% (例: 5.0): ")
            jra_live[v] = {
                'cushion': float(cv),
                'turf_moisture': float(mt),
                'dirt_moisture': float(md),
            }
            print(f"    → CV={cv} 芝={mt}% ダ={md}%")
            print()
    else:
        jra_live = fetch_jra_live()
        for venue, data in jra_live.items():
            c = data.get('cushion', '?')
            tm = data.get('turf_moisture', '?')
            dm = data.get('dirt_moisture', '?')
            print(f"  {venue}: CV={c}  芝={tm}%  ダ={dm}%")
    print()

    # Step 3: クッション値DB読み込み
    print("=" * 60)
    print(f"[Step 3] クッション値DB読み込み")
    print("=" * 60)
    with open(CUSHION_DB_PATH, encoding='utf-8') as f:
        cushion_db = json.load(f)
    print(f"  DB件数: {len(cushion_db)}")
    print()

    # Step 4: 各レース処理
    print("=" * 60)
    print(f"[Step 4] 各レース処理")
    print("=" * 60)
    results_summary = []

    for race in races:
        rid = race['race_id']
        venue = race['venue']
        race_num = race['race_num']
        surface = race['surface']

        print(f"\n--- {venue} {race_num}R {race['race_name']} {surface}{race['distance']}m ---")

        # キャッシュ確認
        cache_file = os.path.join(CACHE_DIR, f'race_{rid}.json')
        if os.path.exists(cache_file) and args.no_scrape:
            print(f"  キャッシュ使用: {cache_file}")
            with open(cache_file, encoding='utf-8') as f:
                race_data = json.load(f)
        elif os.path.exists(cache_file):
            print(f"  キャッシュ使用: {cache_file}")
            with open(cache_file, encoding='utf-8') as f:
                race_data = json.load(f)
        else:
            print(f"  netkeiba スクレイピング中...")
            race_data = scrape_race_data(rid)
            if race_data is None:
                print(f"  SKIP: データ取得失敗")
                continue
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(race_data, f, ensure_ascii=False, indent=2)
            print(f"  キャッシュ保存: {cache_file}")

        # クッション値紐付け
        race_data = link_cushion_data(race_data, cushion_db)

        # 当日クッション値・含水率
        target_cushion = jra_live.get(venue, {}).get('cushion', 9.5)
        if surface == 'ダ':
            target_moisture = jra_live.get(venue, {}).get('dirt_moisture', 5.0)
        else:
            target_moisture = jra_live.get(venue, {}).get('turf_moisture', 12.0)

        # 出力ファイル名
        safe_name = race['race_name'].replace('/', '_').replace(' ', '')
        output_file = os.path.join(out_dir, f'scatter_{venue}{race_num:02d}R_{safe_name}.html')

        pts, with_data, total = generate_scatter_html(
            race_data, target_cushion, target_moisture,
            output_file, date_label=date_label, race_num=race_num,
        )
        print(f"  → 生成完了: {total}頭 ({with_data}頭データあり) {pts}ポイント")
        print(f"  → {output_file}")
        results_summary.append((venue, race_num, race['race_name'], total, pts))

    # サマリー
    print()
    print("=" * 60)
    print("完了サマリー")
    print("=" * 60)
    for venue, rnum, rname, total, pts in results_summary:
        print(f"  {venue}{rnum:2d}R {rname:20s} {total}頭 {pts}pts")
    print(f"\n  出力先: {out_dir}")
    print(f"  合計: {len(results_summary)}レース")

    # インデックスページ生成
    generate_index(out_dir, results_summary, jra_live, date_label)

    # デプロイ
    if args.deploy:
        deploy_to_github(out_dir, date_str)


def generate_index(out_dir, results_summary, jra_live, date_label):
    """レース一覧インデックスページを生成"""
    venues = {}
    for venue, rnum, rname, total, pts in results_summary:
        if venue not in venues:
            venues[venue] = []
        venues[venue].append((rnum, rname, total, pts))

    venue_info = {}
    for venue, data in jra_live.items():
        c = data.get('cushion', '?')
        tm = data.get('turf_moisture', '?')
        dm = data.get('dirt_moisture', '?')
        venue_info[venue] = f'CV={c} 芝{tm}% ダ{dm}%'

    html = f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_label} クッション値×含水率 散布図</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Noto Sans JP',sans-serif; background:#f8fafc; color:#1e293b; padding:16px; }}
h1 {{ font-size:20px; font-weight:900; margin-bottom:4px; }}
.sub {{ font-size:12px; color:#64748b; margin-bottom:16px; }}
.venue {{ margin-bottom:20px; }}
.venue h2 {{ font-size:15px; font-weight:800; padding:10px 14px; background:#1e293b; color:#fff; border-radius:10px 10px 0 0; }}
a {{ display:block; padding:14px 16px; border-bottom:1px solid #e2e8f0; background:#fff; color:#1e293b; text-decoration:none; font-size:15px; font-weight:700; }}
a:last-child {{ border-radius:0 0 10px 10px; }}
a:active {{ background:#f1f5f9; }}
.meta {{ font-size:11px; color:#94a3b8; font-weight:600; }}
a .arrow {{ float:right; color:#94a3b8; }}
</style>
</head>
<body>
<h1>{date_label} クッション値×含水率</h1>
<div class="sub">散布図一覧 ─ タップで各レースの散布図を表示</div>
'''

    for venue in ['東京', '京都', '小倉', '中山', '阪神', '中京', '新潟', '福島', '函館', '札幌']:
        if venue not in venues:
            continue
        info = venue_info.get(venue, '')
        html += f'<div class="venue"><h2>{venue}　{info}</h2>\n'
        for rnum, rname, total, pts in sorted(venues[venue]):
            safe_name = rname.replace('/', '_').replace(' ', '')
            fname = f'scatter_{venue}{rnum:02d}R_{safe_name}.html'
            html += f'<a href="{fname}">{rnum}R {rname} <span class="meta">{total}頭 {pts}pts</span><span class="arrow">→</span></a>\n'
        html += '</div>\n'

    html += '</body></html>'

    index_path = os.path.join(out_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  インデックス: {index_path}")


# ===== GitHub Pages デプロイ =====
DEPLOY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'deploy_config.json')

def deploy_to_github(out_dir, date_str):
    """GitHub Pages へ自動デプロイ（GitHub API使用、git不要）"""
    print()
    print("=" * 60)
    print("[Deploy] GitHub Pages へデプロイ")
    print("=" * 60)

    # 設定読み込み
    if not os.path.exists(DEPLOY_CONFIG_PATH):
        print("  deploy_config.json が見つかりません。")
        print("  以下の形式で作成してください:")
        print('  {"github_token": "ghp_xxx", "repo": "user/repo-name"}')
        return

    with open(DEPLOY_CONFIG_PATH, encoding='utf-8') as f:
        config = json.load(f)

    token = config['github_token']
    repo = config['repo']
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }
    api_base = f'https://api.github.com/repos/{repo}/contents'

    # 既存ファイルのSHA取得（更新に必要）
    print(f"  リポジトリ: {repo}")
    r = requests.get(api_base, headers=headers)
    existing = {}
    if r.status_code == 200:
        for item in r.json():
            existing[item['name']] = item['sha']

    # HTMLファイルをアップロード
    html_files = [f for f in os.listdir(out_dir) if f.endswith('.html')]
    for fname in sorted(html_files):
        fpath = os.path.join(out_dir, fname)
        with open(fpath, 'rb') as f:
            content = base64.b64encode(f.read()).decode()

        encoded_name = quote(fname)
        url = f'{api_base}/{encoded_name}'
        payload = {
            'message': f'Update {fname} ({date_str})',
            'content': content,
        }
        if fname in existing:
            payload['sha'] = existing[fname]

        r = requests.put(url, headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"  ✓ {fname}")
        else:
            try:
                msg = r.json().get('message', '')
            except Exception:
                msg = r.text[:100]
            print(f"  ✗ {fname}: {r.status_code} {msg}")
        time.sleep(1)  # API レートリミット対策

    # 古いファイルを削除（今回の出力にないもの）
    for fname, sha in existing.items():
        if fname.endswith('.html') and fname not in html_files:
            encoded_name = quote(fname)
            url = f'{api_base}/{encoded_name}'
            payload = {'message': f'Remove old file {fname}', 'sha': sha}
            r = requests.delete(url, headers=headers, json=payload)
            if r.status_code == 200:
                print(f"  🗑 {fname} (古いファイル削除)")

    pages_url = f'https://{repo.split("/")[0]}.github.io/{repo.split("/")[1]}/'
    print(f"\n  デプロイ完了！")
    print(f"  📱 スマホでアクセス: {pages_url}")


if __name__ == '__main__':
    main()
