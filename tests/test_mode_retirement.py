#!/usr/bin/env python3
"""mode 退役（2026-07-30）の非対称試験。

背景: removal_mode / counter_mode は 5/31 の手書き規則ファイル（7/06 の構造化置換で
埋葬・コミット 882646e）の消し忘れで、LLM ルーターの自己申告 bit を searcher が
信じる経路だけが宙に浮いて生き残っていた。実害=幻出（「使われて嫌な気分になる
カード」に removal_mode+counter_mode が立ち、採点便 top-20 が火力まみれになった・
2026-07-29 実測）。本人裁定「mode はどこにも要らない。無 mode で性能が落ちるなら
mode の必要性でなく無 mode 設計の欠陥と解釈する」。

この試験が固定するもの:
  A. searcher の公開 API に override 引数が存在しないこと（復活の物理的防止）
  B. 除去/カウンター意図は QUERY_EXPAND（決定的・部分一致）だけが立てること
     ＝正規の客が全員決定的な道から入れること（取り逃し無害・誤発動有害の非対称）
  C. 幻出の再演が構造的に不可能なこと（意図語の無いクエリで mode が立たない）
"""
import inspect
import sys

sys.path.insert(0, '/mnt/mtg_rag/src')
from mtg_hybrid_search_v2 import (MTGHybridSearcherV2, extract_keywords,
                                  detect_intent_modes)

FAILS = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "NG"
    print(f"  [{tag}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def modes(q):
    r = extract_keywords(q)
    return r[4], r[5]  # (removal_mode, counter_mode)


print("== A. override 引数の不在（復活防止）==")
for fn in ("search", "search_with_hyde"):
    params = inspect.signature(getattr(MTGHybridSearcherV2, fn)).parameters
    check(f"{fn}: removal_mode_override 不在", "removal_mode_override" not in params)
    check(f"{fn}: counter_mode_override 不在", "counter_mode_override" not in params)

print("== B. 正規の客は決定的検出で立つ（取り逃しゼロ側）==")
POSITIVE = [
    # (クエリ, 期待 removal, 期待 counter)
    ("単体除去",                       True,  False),
    ("モダンの単体除去",               True,  False),
    ("アトラクサで使える単体除去",     True,  False),  # EDH 便=旧ルーター bit の主客
    ("クリーチャーを破壊する除去",     True,  False),  # ハイブリッド残留 0.909 の主
    ("追放除去",                       True,  False),
    ("全体除去",                       True,  False),
    ("火力",                           True,  False),
    ("カウンター呪文",                 False, True),
    ("確定カウンター呪文",             False, True),
    ("打ち消し呪文",                   False, True),
    ("対抗呪文",                       False, True),
]
for q, rm_want, cm_want in POSITIVE:
    rm, cm = modes(q)
    check(f"「{q}」 rm={rm_want}/cm={cm_want}", rm == rm_want and cm == cm_want,
          f"実際 rm={rm}/cm={cm}")

print("== C. 幻出の再演不可（誤発動ゼロ側・7/29 の実害クエリ込み）==")
NEGATIVE = [
    "使われて嫌な気分になるカード",      # 幻出の実害本人（rm+cm 両方立っていた）
    "墓地からクリーチャーを釣り上げるカード",
    "ミッドレンジといえば",
    "カードを2枚引く",
    "1マナのマナクリーチャー",
    "ならず者",
    "パワーが9以上のクリーチャー",
]
for q in NEGATIVE:
    rm, cm = modes(q)
    check(f"「{q}」 rm=False/cm=False", not rm and not cm, f"実際 rm={rm}/cm={cm}")

print("== D. mode 退役で発覚した取り逃しの是正（工事後の A/B 谷 2 本の再発防止）==")
# 錨ウラモグ: 7B が「アトラクサ対策」へ写し間違え＝原文補完（detect_intent_modes）が命綱
rm, cm = detect_intent_modes("アトラクサを使ったデッキで相手のウラモグに対応できるカード")
check("錨ウラモグ原文 rm=True（「に対応できる」キー）", rm and not cm, f"rm={rm}/cm={cm}")
rm, cm = modes("counter target spell")
check("「counter target spell」 cm=True（英語キー）", cm and not rm, f"rm={rm}/cm={cm}")
rm, cm = modes("counterspell")
check("「counterspell」 cm=True", cm and not rm, f"rm={rm}/cm={cm}")

print("== E. 語彙拡張の誤発動ゼロ（「対策」は除去でない側に固定）==")
for q in ("墓地対策のカード", "アトラクサ対策", "ドラゴン対策になるエンチャント"):
    rm, cm = detect_intent_modes(q)
    check(f"「{q}」 rm=False", not rm, f"実際 rm={rm}")

print()
if FAILS:
    print(f"NG {len(FAILS)} 件: {FAILS}")
    sys.exit(1)
print("mode 退役試験: 全緑")
