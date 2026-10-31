"""通しテスト:CPU だけの対戦を何度も回し、人間の手番も画面クリックで1ゲーム遊ぶ

    ../my-game/.venv/bin/python tests/e2e.py            # 画面なしで実行
    ../my-game/.venv/bin/python tests/e2e.py --headed   # ウィンドウで動きを見る

各ゲームの終了時にルール上の不変条件(トレイ6マス以内・満腹度0〜13・全員2周ゴール等)を確かめる。
スクリーンショットは test-results/ に保存される。
"""
import argparse
import os
import sys
import threading

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dev'))
from serve import make_server, ROOT  # noqa: E402

OUT = os.path.join(ROOT, 'test-results')


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f'  ✓ {msg}', flush=True)


def start(page, url, n, humans):
    page.goto(url)
    page.select_option('#sel-count', str(n))
    seats = page.locator('.seat')
    for i in range(n):
        seats.nth(i).locator('select').select_option('human' if i < humans else 'cpu')
    page.click('#btn-start')


def check_end(page, n):
    page.wait_for_function("window.__state() && window.__state().phase === 'end'", timeout=60_000)
    g = page.evaluate('''() => { const G = window.__state(); return {
        players: G.players.map(p => ({ tray: p.tray.filter(Boolean).length, full: p.full, ranks: p.ranks, pos: p.pos })),
        winners: G.result.winners, totals: G.result.res.map(r => r.s.total) }; }''')
    check(all(p['pos'] == 7 and len(p['ranks']) == 2 for p in g['players']), f'{n}人: 全員が2周ゴール')
    check(all(0 <= p['full'] <= 13 and p['tray'] <= 6 for p in g['players']), f'{n}人: 満腹度とトレイが範囲内')
    check(len(g['winners']) >= 1 and page.is_visible('table.res'), f'{n}人: 結果表を表示 合計={g["totals"]}')


def run(headed):
    os.makedirs(OUT, exist_ok=True)
    srv = make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/?fast'
    errors = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel='chrome', headless=not headed)
        page = br.new_page(viewport={'width': 1200, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: m.type == 'error' and errors.append(m.text))

        print('▶ CPU だけの対戦', flush=True)
        for n in (2, 3, 4):
            for _ in range(5):
                start(page, url, n, 0)
                check_end(page, n)
        check(not errors, 'ページエラーなし ' + '; '.join(errors[:3]))

        print('▶ 人間1人 + CPU3人を画面操作で遊ぶ', flush=True)
        start(page, url, 4, 1)
        page.locator('[data-lunch]').first.click()
        page.screenshot(path=os.path.join(OUT, '1-board.png'), full_page=True)
        shot_place = False
        for _ in range(400):
            st = page.evaluate("() => { const G = window.__state(); return { phase: G.phase, cur: G.current }; }")
            if st['phase'] == 'end':
                break
            if st['cur'] != 0:
                page.wait_for_timeout(20)
                continue
            if st['phase'] == 'place':
                if not shot_place:
                    page.screenshot(path=os.path.join(OUT, '2-place.png'), full_page=True)
                    shot_place = True
                page.locator('[data-cell]').first.click()
            elif page.locator('#action [data-k]').count():
                btns = page.locator('#action [data-k]')
                btns.nth(min(1, btns.count() - 1)).click()   # だいたい1マスずつ進む(0マスがあればおかわり)
            else:
                page.wait_for_timeout(20)
        check_end(page, 4)
        page.screenshot(path=os.path.join(OUT, '3-result.png'), full_page=True)
        check(not errors, 'ページエラーなし ' + '; '.join(errors[:3]))
        br.close()
    if errors:
        print('\n'.join(errors))
        sys.exit(1)
    print('全テスト成功')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--headed', action='store_true')
    run(ap.parse_args().headed)
