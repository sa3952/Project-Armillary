"""渾儀圖版的環幾何生成器。

五個環都是天球上的大圓，平面各由一個單位法向量決定；圖版是正投影。
所以每個環在畫面上必定是「以球心為心、半長軸＝R」的橢圓——這件事不是
畫風選擇，是投影的性質。半短軸與旋轉角由法向量算出，不由眼睛決定。

座標框（右手系）：x̂＝正南　ŷ＝正東　ẑ＝天頂
  地平環 normal = ẑ
  子午環 normal = ŷ            （含天頂與兩極的平面，法向指東）
  赤道環 normal = P            （天北極，地平高度＝緯度 φ，方位正北）
  黃道環 normal = K            （距 P 為 ε 的黃極，繞 P 依恆星時而轉）
"""
import math

D = math.degrees
R_ = math.radians

PHI = 25.053          # 臺北 geonames 代表點緯度 25.053060°N
EPS = 23.4421631      # 23°26′31.79″＝本頁範例曆元（JD(UT) 2448026.7708342，
                      # 1990-05-15 06:30 UT）的真黃赤交角，取自 swe.calc_ut(ECL_NUT)。
                      #
                      # 2026-08-17 更正：先前為 23.440306（23°26′25.1″），來自把平值
                      # 23°26′25.92″ 減去 0.82″ 的推導。兩處都錯——黃赤交角的章動是
                      # 加不是減，且該時刻 Δε = +5.870″ 而非 0.82″：
                      #     平值 23°26′25.92″ ＋ Δε 5.87″ ＝ 真值 23°26′31.79″
                      # 舊值既非真值也非平值，與真值差 6.69″，使圖說「傾角為真值」不成立。
                      # 與圖例、量測記錄同一個值，三處必須一致。

# 視角與時刻。緯度與 ε 是真值；時刻是示意值，選它只為五個環在畫面上分得開。
HOUR_ANGLE = 189.0    # 黃極時角（往西為正）；等效地方恆星時 6h36m
AZIM = -52.0          # 視點方位：由南起算往西 52°
ELEV = 15.0           # 視點仰角


def unit(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def frame(phi_deg):
    p = R_(phi_deg)
    P = (-math.cos(p), 0.0, math.sin(p))          # 天北極
    e1 = (math.sin(p), 0.0, math.cos(p))          # 赤道上時角 0（子午圈南側）
    e2 = (0.0, 1.0, 0.0)                          # 正東
    return P, e1, e2


def ecliptic_normal(phi_deg, eps_deg, hour_angle_deg):
    """黃極：距天極 ε，其時角為 hour_angle_deg（往西為正）。"""
    P, e1, e2 = frame(phi_deg)
    e, h = R_(eps_deg), R_(hour_angle_deg)
    s = math.sin(e)
    return unit(tuple(s * (math.cos(h) * e1[i] - math.sin(h) * e2[i])
                      + math.cos(e) * P[i] for i in range(3)))


def view_basis(azim_deg, elev_deg):
    """v̂ 由場景指向觀者；û 螢幕向右，ŵ 螢幕向上。"""
    a, e = R_(azim_deg), R_(elev_deg)
    v = unit((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)))
    u = unit(cross((0.0, 0.0, 1.0), v))
    w = cross(v, u)
    return v, u, w


def project(pt, basis, cx, cy, scale):
    v, u, w = basis
    return (cx + scale * dot(pt, u), cy - scale * dot(pt, w))


def ring_ellipse(n, basis, cx, cy, R):
    """大圓的投影橢圓：rx=R，ry=R|n·v̂|，旋轉角使短軸對上 n 的畫面投影。"""
    v, u, w = basis
    ry = R * abs(dot(n, v))
    mx, my = dot(n, u), -dot(n, w)              # 法向量在畫面上的方向（y 向下）
    m = math.hypot(mx, my)
    rot = 0.0 if m < 1e-12 else D(math.atan2(-mx / m, my / m))
    return dict(cx=cx, cy=cy, rx=R, ry=ry, rot=rot)


def ring_point(n, t_deg, basis, cx, cy, R):
    """大圓上參數 t 的點，直接投影（用來擺引線錨點，也用來驗證橢圓）。"""
    a = unit(cross(n, (0.0, 0.0, 1.0)) if abs(n[2]) < 0.99 else cross(n, (1.0, 0.0, 0.0)))
    b = cross(n, a)
    t = R_(t_deg)
    p = tuple(R * (math.cos(t) * a[i] + math.sin(t) * b[i]) for i in range(3))
    return project(p, basis, cx, cy, 1.0)


def max_ellipse_error(n, e, basis, cx, cy, R):
    """抽樣真投影點，量它們離解析橢圓有多遠。應該是 0。"""
    worst = 0.0
    for k in range(720):
        px, py = ring_point(n, k * 0.5, basis, cx, cy, R)
        dx, dy = px - e["cx"], py - e["cy"]
        th = R_(e["rot"])
        x = dx * math.cos(th) + dy * math.sin(th)
        y = -dx * math.sin(th) + dy * math.cos(th)
        worst = max(worst, abs((x / e["rx"]) ** 2 + (y / e["ry"]) ** 2 - 1.0))
    return worst


def rings(phi, eps, hk, azim, elev, cx, cy, R):
    P, _, _ = frame(phi)
    basis = view_basis(azim, elev)
    ns = {
        "ecliptic": ecliptic_normal(phi, eps, hk),
        "equator": P,
        "horizon": (0.0, 0.0, 1.0),
        "meridian": (0.0, 1.0, 0.0),
    }
    out = {k: ring_ellipse(n, basis, cx, cy, R) for k, n in ns.items()}
    return ns, out, basis, P


# ── 圖版輸出 ────────────────────────────────────────────────
CX, CY, R = 248.0, 260.0, 196.0
ANCHOR_XMIN = 30      # 錨點至少要在球心右方這麼多，否則水平段會橫穿儀器
ANCHOR_GAP = 22       # 相鄰兩條引線的錨點 y 至少差這麼多
POLE_CLEAR = 22       # 引線與兩極標記的最小距離

ROWS = (52, 156, 260, 364, 468)          # 引線終點 y；與 .legend 五等分列中心對齊
LEAD_END = 598.0

# 圖例順序：1 黃道 2 赤道 3 極軸 4 地平 5 子午
ORDER = ("ecliptic", "equator", "axis", "horizon", "meridian")
STYLE = {                                 # class 名對到 page.css
    "ecliptic": "s-ring hot",
    "equator": "s-ring",
    "horizon": "s-ring soft",
    "meridian": "s-ring",
    "limb": "s-ring soft",
    "axis": "s-ring",
}


def _samples(n, basis, step=0.25):
    return [(t, ring_point(n, t, basis, CX, CY, R))
            for t in [k * step for k in range(int(360 / step))]]


def _axis_samples(P, basis):
    a = project(tuple(-R * c for c in P), basis, CX, CY, 1.0)
    b = project(tuple(R * c for c in P), basis, CX, CY, 1.0)
    return [(k, (a[0] + (b[0] - a[0]) * k / 200.0, a[1] + (b[1] - a[1]) * k / 200.0))
            for k in range(201)], a, b


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _path_clearance(points, q):
    return min(_seg_dist(q, points[i], points[i + 1]) for i in range(len(points) - 1))


def leader_path_points(anchor, row_y):
    """引線的三個轉折點：錨點 → 斜線終點 → 圖例欄。水平段必須在球輪廓之外。"""
    ax, ay = anchor
    dy = abs(row_y - CY)
    clear_x = (CX + math.sqrt(R * R - dy * dy) + 16) if dy < R else CX
    ex = min(max(ax + abs(row_y - ay), ax + 26, clear_x), LEAD_END - 52)
    return [(ax, ay), (ex, row_y), (LEAD_END, row_y)]


def pick_anchors(ns, P, basis, step=0.25):
    """替五條引線挑起點。

    三個硬條件：起點必須真的落在它所指的環上；離其他環至少 10px，否則指了
    也看不出指誰；只取右半球，不然水平段會橫穿整個儀器。
    再加一個軟條件——五個起點的 y 必須依圖例順序遞增，引線才不會互相穿越。
    用 DP 求單調指派下的最佳解，不是逐條貪心。
    """
    pools = {k: _samples(n, basis, step) for k, n in ns.items()}
    pools["axis"] = _axis_samples(P, basis)[0][::max(1, int(step))]
    # 球輪廓不是環，不掛引線，但錨點貼著它一樣看不清楚，所以要一起讓開。
    pools["limb"] = [(t, (CX + R * math.cos(R_(t)), CY + R * math.sin(R_(t))))
                     for t in [k * step for k in range(int(360 / step))]]

    _, pa, pb = _axis_samples(P, basis)

    cands = []
    for idx, key in enumerate(ORDER):
        y_t = min(max(ROWS[idx], 96), 424)
        rows = []
        for _, (x, y) in pools[key]:
            if x < CX + ANCHOR_XMIN:
                continue
            clear = min(math.hypot(x - qx, y - qy)
                        for o, pool in pools.items() if o != key
                        for _, (qx, qy) in pool)
            if clear < 10:
                continue
            # 引線本身也不能擦過兩極標記，否則看起來像指著極點
            if min(_path_clearance(leader_path_points((x, y), ROWS[idx]), q)
                   for q in (pa, pb)) < POLE_CLEAR:
                continue
            rows.append(((x, y), x * 0.55 - abs(y - y_t) * 1.15 + min(clear, 34) * 1.5))
        assert rows, f"{key} 找不到合格的引線錨點"
        rows.sort(key=lambda r: r[0][1])
        cands.append(rows)

    NEG = -1e18
    # 每個 slot 在讀取前都會被下面兩處賦值覆寫；標成 list[list[float]] 是為了
    # 讓型別檢查看得出這一點（[None] * n 會被推成 list[None]）。
    dp: list[list[float]] = [[] for _ in cands]
    dp[-1] = [sc for _, sc in cands[-1]]
    for i in range(len(cands) - 2, -1, -1):
        nxt, dpn = cands[i + 1], dp[i + 1]
        dp[i] = []
        for (pt, sc) in cands[i]:
            bestn = max((dpn[j] for j, (q, _) in enumerate(nxt)
                         if q[1] > pt[1] + ANCHOR_GAP), default=NEG)
            dp[i].append(NEG if bestn == NEG else sc + bestn)
    assert max(dp[0]) > NEG / 2, "找不到不互相穿越的引線配置"

    chosen, prev_y = {}, -1e9
    for i, key in enumerate(ORDER):
        j = max((j for j, (pt, _) in enumerate(cands[i]) if pt[1] > prev_y + ANCHOR_GAP
                 and dp[i][j] > NEG / 2), key=lambda j: dp[i][j])
        chosen[key] = cands[i][j][0]
        prev_y = chosen[key][1]
    return chosen


def leader_path(anchor, row_y):
    (ax, ay), (ex, ey), (fx, _) = leader_path_points(anchor, row_y)
    return f"M{ax:.1f},{ay:.1f} L{ex:.1f},{ey} H{fx:.0f}"


def build(hk, azim, elev, phi=PHI, eps=EPS):
    ns, el, basis, P = rings(phi, eps, hk, azim, elev, CX, CY, R)
    for k, n in ns.items():
        err = max_ellipse_error(n, el[k], basis, CX, CY, R)
        assert err < 1e-9, f"{k} 橢圓與真投影不符：{err}"
        assert abs(el[k]["cx"] - CX) < 1e-9 and abs(el[k]["cy"] - CY) < 1e-9
        assert abs(el[k]["rx"] - R) < 1e-9
    _, pa, pb = _axis_samples(P, basis)
    anchors = pick_anchors(ns, P, basis)

    L = ['<svg viewBox="0 0 640 520" role="img" aria-label="渾儀環組圖版，'
         '編號標示黃道環、赤道環、極軸、地平環與子午環">',
         '  <g>',
         f'    <circle class="{STYLE["limb"]}" cx="{CX:.0f}" cy="{CY:.0f}" r="{R:.0f}"/>']
    for k in ("horizon", "meridian", "equator", "ecliptic"):
        e = el[k]
        L.append(f'    <ellipse class="{STYLE[k]}" cx="{e["cx"]:.0f}" cy="{e["cy"]:.0f}" '
                 f'rx="{e["rx"]:.0f}" ry="{e["ry"]:.2f}" '
                 f'transform="rotate({e["rot"]:.2f} {e["cx"]:.0f} {e["cy"]:.0f})"/>')
    L += [f'    <line class="{STYLE["axis"]}" x1="{pa[0]:.2f}" y1="{pa[1]:.2f}" '
          f'x2="{pb[0]:.2f}" y2="{pb[1]:.2f}"/>',
          f'    <circle class="s-dot" cx="{pa[0]:.2f}" cy="{pa[1]:.2f}" r="3"/>',
          f'    <circle class="s-dot" cx="{pb[0]:.2f}" cy="{pb[1]:.2f}" r="3"/>',
          '  </g>',
          '  <g class="s-lead" fill="none">']
    for idx, k in enumerate(ORDER):
        L.append(f'    <path d="{leader_path(anchors[k], ROWS[idx])}"/>')
    L += ['  </g>', '  <g class="s-dot">']
    for k in ORDER:
        x, y = anchors[k]
        L.append(f'    <circle cx="{x:.1f}" cy="{y:.1f}" r="2.2"/>')
    L += ['  </g>', '  <g class="s-num">']
    for idx in range(5):
        L.append(f'    <text x="610" y="{ROWS[idx] + 4}">{idx + 1}</text>')
    L += ['  </g>', '</svg>']
    return "\n".join(L), el, ns, basis, P


if __name__ == "__main__":
    import sys
    svg, el, ns, basis, P = build(HOUR_ANGLE, AZIM, ELEV)
    if "--check" in sys.argv:
        v, u, w = basis
        print(f"緯度 φ={PHI}°  黃赤交角 ε={EPS}°")
        for k, e in el.items():
            print(f"  {k:9s} rx={e['rx']:.0f} ry={e['ry']:.2f} rot={e['rot']:+.2f}°"
                  f"  扁率={e['ry']/e['rx']:.3f}")
        pairs = [("ecliptic", "equator", EPS), ("meridian", "equator", 90.0),
                 ("meridian", "horizon", 90.0), ("equator", "horizon", 90.0 - PHI)]
        print("  平面二面角（應為右欄）：")
        for a, b, want in pairs:
            got = D(math.acos(min(1.0, abs(dot(ns[a], ns[b])))))
            print(f"    {a}×{b}: {got:.4f}°   應為 {want:.4f}°   差 {abs(got-want):.2e}")
        tilt = D(math.atan2(dot(P, u), dot(P, w)))
        print(f"  極軸偏離鉛垂 {tilt:.2f}°（臺北天極僅 {PHI:.1f}° 仰角，本來就該大幅傾斜）")
    else:
        print(svg)
