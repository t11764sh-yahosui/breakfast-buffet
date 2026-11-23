"""通しテスト:CPU だけの対戦を何度も回し、人間の手番も画面クリックで1ゲーム遊ぶ。
それぞれの端末で遊ぶモード(PeerJS)も、ホストとゲストの2画面で遊ぶ(ネット接続が必要)

    ../my-game/.venv/bin/python tests/e2e.py            # 画面なしで実行
    ../my-game/.venv/bin/python tests/e2e.py --headed   # ウィンドウで動きを見る

各ゲームの終了時にルール上の不変条件(トレイ6マス以内・満腹度0〜13・全員2周ゴール等)を確かめる。
スクリーンショットは test-results/ に保存される。
"""
import argparse
import os
import sys
import threading

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

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
    # ラウンド: ゴール前の全員がちょうど1回ずつ手番を行い、ゴールした人はその周回で手番を行わない
    r = page.evaluate('''() => { const G = window.__state(); const by = {};
        G.turns.forEach(t => { (by[t.lap + '-' + t.round] = by[t.lap + '-' + t.round] || []).push(t); });
        const bad = []; const done = { 1: new Set(), 2: new Set() };
        Object.keys(by).sort((a, b) => { const [l1, r1] = a.split('-').map(Number), [l2, r2] = b.split('-').map(Number); return l1 - l2 || r1 - r2; })
          .forEach(key => { const ts = by[key], lap = ts[0].lap, ids = ts.map(t => t.pi);
            const expect = G.players.map(p => p.i).filter(i => !done[lap].has(i));
            if (ids.length !== new Set(ids).size || ids.length !== expect.length) bad.push(key);
            ts.forEach(t => { if (t.kind === 'goal') done[lap].add(t.pi); }); });
        return { rounds: Object.keys(by).length, bad }; }''')
    check(not r['bad'], f'{n}人: 各ラウンドでゴール前の全員が1回ずつ手番 ({r["rounds"]}ラウンド)')


def no_hscroll(page, where):
    w = page.evaluate('[document.documentElement.scrollWidth, window.innerWidth]')
    check(w[0] <= w[1], f'{where}: 横スクロールなし ({w[0]}px / {w[1]}px)')


def play_human(page, url, tag, phone=False):
    start(page, url, 4, 1)
    if phone:
        no_hscroll(page, 'ランチ選択')
    page.locator('[data-lunch]').first.click()
    page.screenshot(path=os.path.join(OUT, f'{tag}-1-board.png'), full_page=True)
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
                page.screenshot(path=os.path.join(OUT, f'{tag}-2-place.png'))
                if phone:
                    no_hscroll(page, '料理を置く場面')
                    check(page.locator('#action [data-cell]').first.is_visible(), '手番パネル内でお皿を選べる')
                shot_place = True
            page.locator('[data-cell]:visible').first.click()
        elif page.locator('#action [data-k]').count():
            btns = page.locator('#action [data-k]')
            if phone:
                check(btns.first.is_visible(), '移動ボタンが画面内に見えている') if not shot_place else None
            btns.nth(min(1, btns.count() - 1)).click()   # だいたい1マスずつ進む(0マスがあればおかわり)
        else:
            page.wait_for_timeout(20)
    check_end(page, 4)
    if phone:
        no_hscroll(page, '結果画面')
    page.screenshot(path=os.path.join(OUT, f'{tag}-3-result.png'), full_page=True)


NET_TIMEOUT = 20_000   # PeerJS の接続待ちがあるので長め
ERRORS = []


def online_room(br, url):
    """ホスト(PC幅)が部屋をつくり、ゲスト(スマホ幅)が招待リンクから入る。2人 + CPU2人で開始"""
    hctx = br.new_context(viewport={'width': 1200, 'height': 900})
    gctx = br.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
    host, guest = hctx.new_page(), gctx.new_page()
    for pg, who in ((host, 'ホスト'), (guest, 'ゲスト')):
        pg.on('dialog', lambda d: d.accept())
        pg.on('pageerror', lambda e, who=who: ERRORS.append(f'[{who}] {e}'))
        pg.on('console', lambda m, who=who: m.type == 'error' and ERRORS.append(f'[{who}] {m.text}'))
    host.goto(url)
    host.click('.modes [data-mode="online"]')
    host.fill('#net-name', 'ホスト')
    host.click('#btn-host')
    host.wait_for_selector('#lobby:not(.hidden)', timeout=NET_TIMEOUT)
    code = host.inner_text('#room-code')
    guest.goto(f'{url}&room={code}')
    guest.fill('#net-name', 'ゲスト')
    guest.click('#btn-join')
    guest.wait_for_selector('#lobby:not(.hidden)', timeout=NET_TIMEOUT)
    host.wait_for_function("document.querySelectorAll('#members .member:not(.cpu)').length === 2", timeout=NET_TIMEOUT)
    check(True, f'招待コード {code} でゲストが入室')
    no_hscroll(guest, 'スマホの待機室')
    host.select_option('#net-count', '4')
    host.click('#btn-net-start')
    for pg in (host, guest):
        pg.wait_for_selector('[data-lunch]', timeout=NET_TIMEOUT)
        pg.locator('[data-lunch]').first.click()
    return hctx, gctx, host, guest


def online_step(pg):
    """自分の手番なら1手進める。進めたら True"""
    st = pg.evaluate("() => G ? { phase: G.phase, cur: G.current, me: ME, pend: G.pending && G.pending.pi } : null")
    if not st:
        return False
    try:   # 同期で画面が描き直されてボタンが消えることがあるので、押せなければ次の機会に回す
        if st['phase'] == 'place' and st['pend'] == st['me']:
            pg.locator('[data-cell]:visible').first.click(timeout=2000)
            return True
        if st['phase'] == 'play' and st['cur'] == st['me'] and pg.locator('#action [data-k]').count():
            btns = pg.locator('#action [data-k]')
            btns.nth(min(1, btns.count() - 1)).click(timeout=2000)
            return True
    except PwTimeout:
        pass
    return False


def play_online(br, url):
    hctx, gctx, host, guest = online_room(br, url)
    host_lunch = host.evaluate('G.players[0].lunch.name')
    guest.wait_for_function("G && G.phase === 'play'", timeout=NET_TIMEOUT)
    check(host_lunch not in guest.inner_text('body'), 'ゲストの画面にホストのランチ予定は出ない')
    check(guest.evaluate("G.players[0].lunch.hidden === true && G.deck.every(c => c === null)"), 'ゲストには山札や他人のランチの中身が届かない')
    shot = False
    for _ in range(3000):
        if host.evaluate("G.phase") == 'end':
            break
        moved = online_step(host) or online_step(guest)
        if moved and not shot and guest.evaluate("G.current === ME"):
            guest.screenshot(path=os.path.join(OUT, 'online-guest-turn.png'))
            shot = True
        if not moved:
            host.wait_for_timeout(15)
    check_end(host, 4)
    guest.wait_for_selector('table.res', timeout=NET_TIMEOUT)
    totals = [pg.evaluate('G.result.res.map(r => r.s.total)') for pg in (host, guest)]
    check(totals[0] == totals[1], f'ホストとゲストで同じ結果 {totals[0]}')
    no_hscroll(guest, 'ゲストの結果画面')
    guest.screenshot(path=os.path.join(OUT, 'online-guest-result.png'), full_page=True)
    # もう一度遊ぶ → 両方とも待機室へ
    host.click('#btn-again')
    guest.wait_for_selector('#lobby:not(.hidden)', timeout=NET_TIMEOUT)
    check(host.is_visible('#lobby'), '「もう一度遊ぶ」で2人とも待機室に戻る')
    hctx.close(); gctx.close()


def online_drop(br, url):
    """ゲストが途中で抜けても CPU が代わりに遊んで最後まで進む"""
    hctx, gctx, host, guest = online_room(br, url)
    turns = 0
    while turns < 3:
        if online_step(guest):
            turns += 1
        elif not online_step(host):
            host.wait_for_timeout(15)
    gctx.close()
    host.wait_for_function("G.players[1].cpu && G.players[1].dropped", timeout=NET_TIMEOUT)
    check(True, 'ゲストが抜けたら CPU に交代')
    # 同じ名前で入り直すと席に戻れる
    code = host.inner_text('#room-code')
    gctx = br.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
    guest = gctx.new_page()
    guest.on('pageerror', lambda e: ERRORS.append(f'[ゲスト再入室] {e}'))
    guest.goto(f'{url}&room={code}')
    guest.fill('#net-name', 'ゲスト')
    guest.click('#btn-join')
    guest.wait_for_function("G && ME === 1 && !G.players[1].cpu", timeout=NET_TIMEOUT)
    check(guest.is_visible('#game') and host.evaluate("!G.players[1].cpu"), '同じ名前で入り直すと自分の席に戻る')
    for _ in range(3000):
        if host.evaluate("G.phase") == 'end':
            break
        if not (online_step(host) or online_step(guest)):
            host.wait_for_timeout(15)
    check_end(host, 4)
    guest.wait_for_selector('table.res', timeout=NET_TIMEOUT)
    hctx.close(); gctx.close()


def run(headed):
    os.makedirs(OUT, exist_ok=True)
    srv = make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{srv.server_address[1]}/?fast'
    errors = ERRORS
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

        print('▶ 人間1人 + CPU3人を画面操作で遊ぶ(PC)', flush=True)
        play_human(page, url, 'pc')
        check(not errors, 'ページエラーなし ' + '; '.join(errors[:3]))

        print('▶ 同じくスマホ幅(390×844、タッチ)で遊ぶ', flush=True)
        ctx = br.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
        phone = ctx.new_page()
        phone.on('pageerror', lambda e: errors.append(str(e)))
        phone.on('console', lambda m: m.type == 'error' and errors.append(m.text))
        play_human(phone, url, 'sp', phone=True)
        check(not errors, 'ページエラーなし ' + '; '.join(errors[:3]))

        print('▶ それぞれの端末で遊ぶ(ホスト + ゲスト + CPU2人、PeerJS で本物の通信)', flush=True)
        play_online(br, url)
        print('▶ ゲストが途中で抜けて、入り直す', flush=True)
        online_drop(br, url)
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
