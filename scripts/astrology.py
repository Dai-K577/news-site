"""
Astrology Chart & Forecast (完全ローカル動作)
- 太陽〜冥王星+月の黄道座標をローカル計算(外部API・外部ライブラリ不要)
- 惑星: JPL近似ケプラー軌道要素(1800〜2050年で高精度)
- 月: Schlyter法(主要摂動項入り、誤差2分角程度)
- 12星座位置 / アスペクト / 逆行判定 / エレメントバランス
- 今後のイベント予測(イングレス・逆行転換・新月満月・アスペクト成立)と考察

使い方:
    python scripts/astrology.py                        # 現在時刻(JST)のチャート + 30日予測
    python scripts/astrology.py --date 2026-07-11
    python scripts/astrology.py --date 2026-07-11 --time 12:00 --tz 9
    python scripts/astrology.py --days 90              # 予測期間を90日に
    python scripts/astrology.py --json                 # JSON出力
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

DEG = math.pi / 180.0


# ─────────────────────────────────────────
# 時刻 → ユリウス日
# ─────────────────────────────────────────
def julian_day(dt_utc: datetime) -> float:
    """UTCのdatetimeからユリウス日を求める"""
    y, m = dt_utc.year, dt_utc.month
    d = (dt_utc.day
         + dt_utc.hour / 24.0
         + dt_utc.minute / 1440.0
         + dt_utc.second / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


# ─────────────────────────────────────────
# 惑星の軌道要素 (JPL近似暦: J2000黄道基準, 1800-2050年)
# 各要素: (J2000での値, 1世紀あたりの変化率)
# a[au], e, i[deg], L[deg], 近日点黄経[deg], 昇交点黄経[deg]
# ─────────────────────────────────────────
PLANET_ELEMENTS = {
    "水星": (
        (0.38709927, 0.00000037), (0.20563593, 0.00001906),
        (7.00497902, -0.00594749), (252.25032350, 149472.67411175),
        (77.45779628, 0.16047689), (48.33076593, -0.12534081)),
    "金星": (
        (0.72333566, 0.00000390), (0.00677672, -0.00004107),
        (3.39467605, -0.00078890), (181.97909950, 58517.81538729),
        (131.60246718, 0.00268329), (76.67984255, -0.27769418)),
    "地球": (  # 地球-月重心
        (1.00000261, 0.00000562), (0.01671123, -0.00004392),
        (-0.00001531, -0.01294668), (100.46457166, 35999.37244981),
        (102.93768193, 0.32327364), (0.0, 0.0)),
    "火星": (
        (1.52371034, 0.00001847), (0.09339410, 0.00007882),
        (1.84969142, -0.00813131), (-4.55343205, 19140.30268499),
        (-23.94362959, 0.44441088), (49.55953891, -0.29257343)),
    "木星": (
        (5.20288700, -0.00011607), (0.04838624, -0.00013253),
        (1.30439695, -0.00183714), (34.39644051, 3034.74612775),
        (14.72847983, 0.21252668), (100.47390909, 0.20469106)),
    "土星": (
        (9.53667594, -0.00125060), (0.05386179, -0.00050991),
        (2.48599187, 0.00193609), (49.95424423, 1222.49362201),
        (92.59887831, -0.41897216), (113.66242448, -0.28867794)),
    "天王星": (
        (19.18916464, -0.00196176), (0.04725744, -0.00004397),
        (0.77263783, -0.00242939), (313.23810451, 428.48202785),
        (170.95427630, 0.40805281), (74.01692503, 0.04240589)),
    "海王星": (
        (30.06992276, 0.00026291), (0.00859048, 0.00005105),
        (1.77004347, 0.00035372), (-55.12002969, 218.45945325),
        (44.96476227, -0.32241464), (131.78422574, -0.00508664)),
    "冥王星": (
        (39.48211675, -0.00031596), (0.24882730, 0.00005170),
        (17.14001206, 0.00004818), (238.92903833, 145.20780515),
        (224.06891629, -0.04062942), (110.30393684, -0.01183482)),
}


def _solve_kepler(M_rad: float, e: float) -> float:
    """ケプラー方程式 M = E - e*sinE をニュートン法で解く"""
    E = M_rad + e * math.sin(M_rad)
    for _ in range(20):
        dE = (E - e * math.sin(E) - M_rad) / (1.0 - e * math.cos(E))
        E -= dE
        if abs(dE) < 1e-12:
            break
    return E


def _heliocentric_xyz(name: str, T: float) -> tuple[float, float, float]:
    """惑星の日心黄道座標(J2000基準) T: J2000からのユリウス世紀"""
    el = PLANET_ELEMENTS[name]
    a = el[0][0] + el[0][1] * T
    e = el[1][0] + el[1][1] * T
    inc = (el[2][0] + el[2][1] * T) * DEG
    L = el[3][0] + el[3][1] * T
    varpi = el[4][0] + el[4][1] * T
    Omega = (el[5][0] + el[5][1] * T) * DEG

    M = math.radians((L - varpi) % 360.0)
    omega = math.radians(varpi) - Omega

    E = _solve_kepler(M, e)
    xp = a * (math.cos(E) - e)
    yp = a * math.sqrt(1.0 - e * e) * math.sin(E)

    cw, sw = math.cos(omega), math.sin(omega)
    cO, sO = math.cos(Omega), math.sin(Omega)
    ci, si = math.cos(inc), math.sin(inc)

    x = (cw * cO - sw * sO * ci) * xp + (-sw * cO - cw * sO * ci) * yp
    y = (cw * sO + sw * cO * ci) * xp + (-sw * sO + cw * cO * ci) * yp
    z = (sw * si) * xp + (cw * si) * yp
    return x, y, z


def _precession(T: float) -> float:
    """J2000黄道座標 → その日付の春分点基準への補正[deg]"""
    return 1.396971 * T + 0.0003086 * T * T


def planet_longitude(name: str, jd: float) -> float:
    """地心黄経[deg](その日付の春分点基準=トロピカル)"""
    T = (jd - 2451545.0) / 36525.0
    ex, ey, ez = _heliocentric_xyz("地球", T)
    if name == "太陽":
        gx, gy = -ex, -ey
    else:
        px, py, pz = _heliocentric_xyz(name, T)
        gx, gy = px - ex, py - ey
    lon = math.degrees(math.atan2(gy, gx)) + _precession(T)
    return lon % 360.0


# ─────────────────────────────────────────
# 月の位置 (Schlyter法, 摂動項入り)
# ─────────────────────────────────────────
def moon_longitude(jd: float) -> float:
    """月の地心黄経[deg](その日付の春分点基準)"""
    d = jd - 2451543.5

    N = 125.1228 - 0.0529538083 * d      # 昇交点黄経
    w = 318.0634 + 0.1643573223 * d      # 近地点引数
    e = 0.054900
    M = 115.3654 + 13.0649929509 * d     # 平均近点角

    Ms = 356.0470 + 0.9856002585 * d     # 太陽の平均近点角
    ws = 282.9404 + 4.70935e-5 * d
    Ls = Ms + ws                         # 太陽の平均黄経

    E = _solve_kepler(math.radians(M % 360.0), e)
    xp = math.cos(E) - e
    yp = math.sqrt(1.0 - e * e) * math.sin(E)
    v = math.degrees(math.atan2(yp, xp))  # 真近点角

    lon = (N + w + v) % 360.0            # 摂動なし黄経

    Lm = (N + w + M) % 360.0             # 月の平均黄経
    D = math.radians(Lm - Ls)            # 平均離角
    F = math.radians(Lm - N)             # 昇交点からの引数
    Mr, Msr = math.radians(M), math.radians(Ms)

    lon += (
        -1.274 * math.sin(Mr - 2 * D)          # 出差
        + 0.658 * math.sin(2 * D)              # 二均差
        - 0.186 * math.sin(Msr)                # 年差
        - 0.059 * math.sin(2 * Mr - 2 * D)
        - 0.057 * math.sin(Mr - 2 * D + Msr)
        + 0.053 * math.sin(Mr + 2 * D)
        + 0.046 * math.sin(2 * D - Msr)
        + 0.041 * math.sin(Mr - Msr)
        - 0.035 * math.sin(D)                  # 月角差
        - 0.031 * math.sin(Mr + Msr)
        - 0.015 * math.sin(2 * F - 2 * D)
        + 0.011 * math.sin(Mr - 4 * D)
    )
    return lon % 360.0


# ─────────────────────────────────────────
# 天体・星座・アスペクト定義
# ─────────────────────────────────────────
BODIES = ["太陽", "月", "水星", "金星", "火星", "木星", "土星", "天王星", "海王星", "冥王星"]

SIGNS = ["牡羊座", "牡牛座", "双子座", "蟹座", "獅子座", "乙女座",
         "天秤座", "蠍座", "射手座", "山羊座", "水瓶座", "魚座"]

SIGN_ELEMENTS = ["火", "地", "風", "水"] * 3  # 牡羊=火, 牡牛=地, 双子=風, 蟹=水, ...

SIGN_MEANINGS = {
    "牡羊座": "開拓・スタート・勢い",
    "牡牛座": "安定・所有・五感",
    "双子座": "情報・コミュニケーション・好奇心",
    "蟹座": "感情・家庭・共感",
    "獅子座": "自己表現・創造・情熱",
    "乙女座": "分析・実務・調整",
    "天秤座": "調和・対人関係・バランス",
    "蠍座": "深化・変容・集中",
    "射手座": "拡大・探求・楽観",
    "山羊座": "達成・組織・責任",
    "水瓶座": "革新・独立・ネットワーク",
    "魚座": "直感・共感・想像力",
}

BODY_MEANINGS = {
    "太陽": "人生の目的・自我",
    "月": "感情・日常のリズム",
    "水星": "知性・伝達・商取引",
    "金星": "愛情・美・金銭",
    "火星": "行動力・闘争心",
    "木星": "発展・幸運・拡大",
    "土星": "試練・構築・責任",
    "天王星": "変革・独創・突発",
    "海王星": "夢・霊感・曖昧さ",
    "冥王星": "根本的変容・再生",
}

# (角度, 名称, 記号, 許容オーブ, 性質)
ASPECTS = [
    (0, "コンジャンクション(合)", "☌", 8.0, "強調"),
    (60, "セクスタイル", "⚹", 5.0, "調和"),
    (90, "スクエア", "□", 7.0, "緊張"),
    (120, "トライン", "△", 7.0, "調和"),
    (180, "オポジション(衝)", "☍", 8.0, "緊張"),
]

ASPECT_TEXT = {
    "強調": "エネルギーが融合し合い、そのテーマが強く前面に出ます",
    "調和": "スムーズに力を発揮でき、チャンスに恵まれやすい配置です",
    "緊張": "葛藤や摩擦を通じて成長を促す、challenge の配置です",
}


def body_longitude(name: str, jd: float) -> float:
    if name == "月":
        return moon_longitude(jd)
    return planet_longitude(name, jd)


def body_speed(name: str, jd: float) -> float:
    """黄経の日運動量[deg/day](負なら逆行)"""
    dl = angle_diff(body_longitude(name, jd + 0.5), body_longitude(name, jd - 0.5))
    return dl


def angle_diff(a: float, b: float) -> float:
    """a-b を -180..180 に正規化"""
    return (a - b + 180.0) % 360.0 - 180.0


def sign_of(lon: float) -> tuple[str, float]:
    """黄経 → (星座名, 星座内度数)"""
    idx = int(lon // 30) % 12
    return SIGNS[idx], lon % 30.0


def fmt_deg(deg_in_sign: float) -> str:
    d = int(deg_in_sign)
    m = int(round((deg_in_sign - d) * 60))
    if m == 60:
        d, m = d + 1, 0
    return f"{d:2d}°{m:02d}′"


# ─────────────────────────────────────────
# チャート計算
# ─────────────────────────────────────────
def compute_chart(jd: float) -> dict:
    positions = {}
    for name in BODIES:
        lon = body_longitude(name, jd)
        speed = body_speed(name, jd)
        sign, deg_in = sign_of(lon)
        positions[name] = {
            "longitude": round(lon, 3),
            "sign": sign,
            "degree": round(deg_in, 3),
            "speed": round(speed, 4),
            "retrograde": bool(speed < 0 and name not in ("太陽", "月")),
        }

    aspects = []
    for i, n1 in enumerate(BODIES):
        for n2 in BODIES[i + 1:]:
            sep = abs(angle_diff(positions[n1]["longitude"], positions[n2]["longitude"]))
            for angle, aname, sym, orb, nature in ASPECTS:
                if abs(sep - angle) <= orb:
                    aspects.append({
                        "body1": n1, "body2": n2,
                        "aspect": aname, "symbol": sym,
                        "angle": angle, "orb": round(abs(sep - angle), 2),
                        "nature": nature,
                    })
                    break
    aspects.sort(key=lambda a: a["orb"])
    return {"positions": positions, "aspects": aspects}


# ─────────────────────────────────────────
# 今後のイベント検出
# ─────────────────────────────────────────
def _refine_crossing(fn, jd_lo: float, jd_hi: float) -> float:
    """fn(jd)の符号が変わる区間を二分法で絞り込む"""
    f_lo = fn(jd_lo)
    for _ in range(24):
        mid = (jd_lo + jd_hi) / 2.0
        f_mid = fn(mid)
        if f_lo * f_mid <= 0:
            jd_hi = mid
        else:
            jd_lo, f_lo = mid, f_mid
    return (jd_lo + jd_hi) / 2.0


def scan_events(jd_start: float, days: int) -> list[dict]:
    """イングレス・逆行転換・新月満月・アスペクト成立を日単位でスキャン"""
    events = []
    step = 1.0
    n_steps = int(days / step)

    # 前回状態の初期化
    prev = {}
    for name in BODIES:
        lon = body_longitude(name, jd_start)
        prev[name] = {"sign": int(lon // 30) % 12, "speed": body_speed(name, jd_start)}
    prev_elong = angle_diff(moon_longitude(jd_start), planet_longitude("太陽", jd_start))
    prev_asp = {}
    slow = [b for b in BODIES if b != "月"]
    for i, n1 in enumerate(slow):
        for n2 in slow[i + 1:]:
            sep = angle_diff(body_longitude(n1, jd_start), body_longitude(n2, jd_start))
            prev_asp[(n1, n2)] = sep

    for k in range(1, n_steps + 1):
        jd = jd_start + k * step
        jd0 = jd - step

        # ① サイン・イングレス(星座の移動)
        for name in BODIES:
            lon = body_longitude(name, jd)
            sidx = int(lon // 30) % 12
            p_sidx = prev[name]["sign"]
            if sidx != p_sidx:
                # 順行なら新サインの始点、逆行なら旧サインの始点が越えた境界
                if (p_sidx + 1) % 12 == sidx:
                    tgt = sidx * 30.0
                else:
                    tgt = p_sidx * 30.0

                def f_ing(j, nm=name, t=tgt):
                    return angle_diff(body_longitude(nm, j), t)
                jd_x = _refine_crossing(f_ing, jd0, jd)
                new_sign = sign_of(body_longitude(name, jd_x + 1e-4))[0]
                events.append({
                    "jd": jd_x, "type": "ingress", "body": name, "sign": new_sign,
                    "text": f"{name}が{new_sign}に移動 — {BODY_MEANINGS[name]}のテーマが"
                            f"「{SIGN_MEANINGS[new_sign]}」の色合いを帯び始めます",
                })
                prev[name]["sign"] = sidx

        # ② 逆行の開始・終了(太陽・月を除く)
        for name in BODIES:
            if name in ("太陽", "月"):
                continue
            spd = body_speed(name, jd)
            if spd * prev[name]["speed"] < 0:
                def f_st(j, nm=name):
                    return body_speed(nm, j)
                jd_x = _refine_crossing(f_st, jd0, jd)
                if spd < 0:
                    events.append({
                        "jd": jd_x, "type": "retro_start", "body": name,
                        "text": f"{name}が逆行開始 — {BODY_MEANINGS[name]}に関する見直し・"
                                f"再点検の期間に入ります",
                    })
                else:
                    events.append({
                        "jd": jd_x, "type": "retro_end", "body": name,
                        "text": f"{name}が順行に復帰 — 停滞していた{BODY_MEANINGS[name]}の"
                                f"物事が前進し始めます",
                    })
            prev[name]["speed"] = spd

        # ③ 新月・満月
        elong = angle_diff(moon_longitude(jd), planet_longitude("太陽", jd))
        if prev_elong < 0 <= elong:  # 0°通過 = 新月
            def f_nm(j):
                return angle_diff(moon_longitude(j), planet_longitude("太陽", j))
            jd_x = _refine_crossing(f_nm, jd0, jd)
            sign = sign_of(moon_longitude(jd_x))[0]
            events.append({
                "jd": jd_x, "type": "new_moon", "body": "月", "sign": sign,
                "text": f"{sign}で新月 — 「{SIGN_MEANINGS[sign]}」に関する物事を"
                        f"新しく始めるのに適したタイミングです",
            })
        # 満月: 離角の180°通過 → (離角-180)の符号変化で検出(wrap考慮)
        p180 = angle_diff(prev_elong, 180.0)
        c180 = angle_diff(elong, 180.0)
        if p180 < 0 <= c180:
            def f_fm(j):
                return angle_diff(
                    angle_diff(moon_longitude(j), planet_longitude("太陽", j)), 180.0)
            jd_x = _refine_crossing(f_fm, jd0, jd)
            sign = sign_of(moon_longitude(jd_x))[0]
            events.append({
                "jd": jd_x, "type": "full_moon", "body": "月", "sign": sign,
                "text": f"{sign}で満月 — 「{SIGN_MEANINGS[sign]}」に関する物事が"
                        f"成果・結実を迎え、感情も高まりやすい時です",
            })
        prev_elong = elong

        # ④ 惑星間アスペクトの成立(月以外・タイトな成立瞬間)
        for i, n1 in enumerate(slow):
            for n2 in slow[i + 1:]:
                sep = angle_diff(body_longitude(n1, jd), body_longitude(n2, jd))
                psep = prev_asp[(n1, n2)]
                for angle, aname, sym, orb, nature in ASPECTS:
                    targets = {float(angle)} if angle in (0, 180) else {float(angle), -float(angle)}
                    for tgt in targets:
                        d_prev = angle_diff(psep, tgt)
                        d_cur = angle_diff(sep, tgt)
                        if d_prev * d_cur < 0 and abs(d_prev) < 3 and abs(d_cur) < 3:
                            def f_asp(j, a=n1, b=n2, t=tgt):
                                return angle_diff(
                                    angle_diff(body_longitude(a, j), body_longitude(b, j)), t)
                            jd_x = _refine_crossing(f_asp, jd0, jd)
                            events.append({
                                "jd": jd_x, "type": "aspect", "body": f"{n1}-{n2}",
                                "aspect": aname, "nature": nature,
                                "text": f"{n1}と{n2}が{aname}{sym}を形成 — "
                                        f"{BODY_MEANINGS[n1]}と{BODY_MEANINGS[n2]}の間で"
                                        f"{ASPECT_TEXT[nature]}",
                            })
                prev_asp[(n1, n2)] = sep

    events.sort(key=lambda e: e["jd"])
    return events


# ─────────────────────────────────────────
# 考察テキスト生成
# ─────────────────────────────────────────
def element_balance(chart: dict) -> dict:
    counts = {"火": 0, "地": 0, "風": 0, "水": 0}
    for name, pos in chart["positions"].items():
        counts[SIGN_ELEMENTS[SIGNS.index(pos["sign"])]] += 1
    return counts

ELEMENT_TEXT = {
    "火": "行動力と情熱が高まりやすく、新しい挑戦に向く時期",
    "地": "現実的な積み上げや実務・金銭面の整備に向く時期",
    "風": "情報交換・学び・人とのつながりが活発になる時期",
    "水": "感情や直感が優位になり、内面と向き合いやすい時期",
}


def build_commentary(chart: dict) -> list[str]:
    lines = []

    balance = element_balance(chart)
    dominant = max(balance, key=balance.get)
    lines.append(
        f"エレメントバランスは 火{balance['火']}・地{balance['地']}・"
        f"風{balance['風']}・水{balance['水']}。"
        f"「{dominant}」が優勢で、{ELEMENT_TEXT[dominant]}といえます。")

    retro = [n for n, p in chart["positions"].items() if p["retrograde"]]
    if retro:
        lines.append(
            f"現在逆行中の天体: {'、'.join(retro)}。"
            "逆行中はその天体が司る事柄(" +
            "、".join(BODY_MEANINGS[n] for n in retro) +
            ")について、新規の推進よりも見直し・修復が実を結びやすい配置です。")
    else:
        lines.append("現在逆行中の天体はなく、全天体順行。物事を前へ進めやすい時期です。")

    tight = [a for a in chart["aspects"] if a["orb"] <= 3.0][:5]
    for a in tight:
        lines.append(
            f"{a['body1']} {a['symbol']} {a['body2']}"
            f"({a['aspect']}, オーブ{a['orb']}°): "
            f"{BODY_MEANINGS[a['body1']]}と{BODY_MEANINGS[a['body2']]}の間で"
            f"{ASPECT_TEXT[a['nature']]}。")

    return lines


# ─────────────────────────────────────────
# 出力
# ─────────────────────────────────────────
def jd_to_jst(jd: float) -> datetime:
    days = jd - 2440587.5
    return datetime.fromtimestamp(days * 86400.0, tz=timezone.utc).astimezone(JST)


def print_report(dt_local: datetime, chart: dict, events: list[dict], days: int):
    line = "─" * 58
    print(line)
    print(f"  ホロスコープ  {dt_local.strftime('%Y-%m-%d %H:%M %Z')}")
    print(line)

    print("\n【天体位置】")
    for name in BODIES:
        p = chart["positions"][name]
        r = " (逆行)" if p["retrograde"] else ""
        print(f"  {name:　<4} {p['sign']:　<4} {fmt_deg(p['degree'])}  "
              f"(黄経 {p['longitude']:7.2f}°){r}")

    print("\n【アスペクト】")
    if not chart["aspects"]:
        print("  該当なし")
    for a in chart["aspects"]:
        print(f"  {a['body1']:　<4}{a['symbol']} {a['body2']:　<4} "
              f"{a['aspect']:<16} オーブ {a['orb']:.1f}° [{a['nature']}]")

    print("\n【総合考察】")
    for s in build_commentary(chart):
        print(f"  ・{s}")

    print(f"\n【今後{days}日間の主要イベントと考察】")
    if not events:
        print("  該当イベントなし")
    for ev in events:
        t = jd_to_jst(ev["jd"])
        print(f"  {t.strftime('%m/%d %H:%M')}  {ev['text']}")
    print(line)


def main():
    parser = argparse.ArgumentParser(
        description="占星術チャート計算と今後の考察(完全ローカル動作)")
    parser.add_argument("--date", help="日付 YYYY-MM-DD (省略時: 今日)")
    parser.add_argument("--time", default="12:00", help="時刻 HH:MM (既定: 12:00)")
    parser.add_argument("--tz", type=float, default=9.0,
                        help="タイムゾーン(UTCからの時差, 既定: 9=JST)")
    parser.add_argument("--days", type=int, default=30,
                        help="予測期間の日数 (既定: 30)")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    args = parser.parse_args()

    tz = timezone(timedelta(hours=args.tz))
    if args.date:
        try:
            hh, mm = map(int, args.time.split(":"))
            y, m, d = map(int, args.date.split("-"))
            dt_local = datetime(y, m, d, hh, mm, tzinfo=tz)
        except ValueError:
            print("日付/時刻の形式が不正です (例: --date 2026-07-11 --time 12:00)",
                  file=sys.stderr)
            sys.exit(1)
    else:
        dt_local = datetime.now(tz)

    if not (1800 <= dt_local.year <= 2050):
        print("対応範囲は1800〜2050年です(軌道要素の有効期間)", file=sys.stderr)
        sys.exit(1)

    jd = julian_day(dt_local.astimezone(timezone.utc))
    chart = compute_chart(jd)
    events = scan_events(jd, args.days)

    if args.json:
        out = {
            "datetime": dt_local.isoformat(),
            "julian_day": round(jd, 5),
            "positions": chart["positions"],
            "aspects": chart["aspects"],
            "commentary": build_commentary(chart),
            "events": [
                {**ev, "datetime_jst": jd_to_jst(ev["jd"]).isoformat(),
                 "jd": round(ev["jd"], 5)}
                for ev in events
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print_report(dt_local, chart, events, args.days)


if __name__ == "__main__":
    main()
