"""The timeline on bome.narrowcap6721.near's real sequence (12 transfers, read off NearBlocks)."""

from rugguard.timeline import build, render

from .test_outcome import POOL, t

D = 10**6           # bome has 6 decimals in this fixture


def bome():
    m = 1 / 60      # one minute, in hours
    return [
        t(-17 * m, None, "narrowcap6721.near", 1_000_000 * D, "mint"),
        t(0, "narrowcap6721.near", POOL, 900_000 * D, "seed"),
        t(2 * m, POOL, "glacial.near", 46_786 * D),
        t(7 * m, "glacial.near", POOL, 23_393 * D),
        t(7.2 * m, POOL, "narrowcap6721.near", 9_355 * D, "selfbuy"),
        t(8.5 * m, POOL, "frigid.near", 9_067 * D),
        t(13 * m, POOL, "b196.near", 135_903 * D),
        t(14.5 * m, POOL, "kabirty.near", 6_297 * D),
        t(15.4 * m, "b196.near", POOL, 135_903 * D),
        t(23 * m, "kabirty.near", POOL, 6_297 * D),
        t(24.6 * m, POOL, "narrowcap6721.near", 858_073 * D, "pull"),
        t(25 * m, "x.near", "wallet.intear.near", 1 * D, "fee"),
        t(4 * 24, "frigid.near", POOL, 9_067 * D, "stranded"),
    ]


def test_bome_timeline_marks_seed_pull_round_trips_and_fees():
    tl = build(bome())
    assert tl["seeder"] == "narrowcap6721.near" and tl["seed"] == 900_000 * D
    kinds = [(r.kind, r.amount // D) for r in tl["rows"]]
    assert ("SEED", 900_000) in kinds and ("PULL?", 858_073) in kinds
    assert ("seeder buy", 9_355) in kinds                  # small self-buy is not a pull
    assert tl["hidden"]["round trip"] == 4                 # b196 and kabirty bought and sold back
    assert tl["hidden"]["fee"] == 1
    assert ("sell", 9_067) in kinds                        # the stranded holder, days later
    text = render(tl, 6)
    assert "PULL?" in text and "95% of S" in text and "+25m" in text and "+4.0d" in text


def test_no_pull_is_said_plainly():
    tr = [x for x in bome() if x.tx != "pull"]
    assert "No pool -> seeder transfer" in render(build(tr), 6)


def test_small_trades_fold_into_a_count():
    tr = bome() + [t(30, POOL, f"tiny{i}.near", 10 * D) for i in range(500)]
    tl = build(tr)
    assert tl["hidden"]["small buy"] == 500 and len(tl["rows"]) < 15


def test_arbitrage_bot_seeder_is_flagged_and_dcl_is_a_pool():
    """kinu-3: the first v2 depositor shuttles tokens between the DCL pool and the v2 pool."""
    dcl, bot = "dclv2.ref-labs.near", "kulesh.near"
    m = 1 / 60
    tr = [t(-1, None, "pad.near", 1_000_000_000 * D, "mint")]
    for k in range(4):
        h = k * 0.5
        tr += [t(h, dcl, bot, 12_000_000 * D, f"a{k}"), t(h + m, bot, POOL, 11_900_000 * D, f"b{k}"),
               t(h + 10 * m, POOL, bot, 9_000_000 * D, f"c{k}"), t(h + 11 * m, bot, dcl, 8_950_000 * D, f"d{k}")]
    tl = build(sorted(tr, key=lambda x: x.ts))
    assert tl["seeder"] == bot and tl["arbitrage_legs"] >= 3
    assert any(r.kind == "seeder <- dclv2" for r in tl["rows"])
    assert "arbitrage bot" in render(tl, 6)


def test_many_small_mints_fold_into_a_count():
    tr = bome() + [t(-20 + i / 100, None, "minter.near", 50 * D) for i in range(1000)]
    tl = build(sorted(tr, key=lambda x: x.ts))
    assert tl["hidden"]["small mint"] == 1000
