"""DEX token/factory registry + curated fallback pools (engine *data* input, §3.3/§3.4).

Token addresses, decimals and factory addresses for verified assets per network. Pools
are DISCOVERED at runtime by querying the V2 factory's getPair() for every verified
base × quote combination (see v2_discovery) — the curated lists below are only the
cold-start fallback used until (or if) on-chain discovery succeeds.
V3 concentrated-liquidity reading is a documented future extension (§5.4); MVP models
constant-product (V2) pools whose getReserves() gives exact slippage.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PoolDef:
    base_asset: str
    quote_asset: str
    pool_address: str
    token0_is_base: bool
    base_decimals: int
    quote_decimals: int
    fee_tier: Decimal


@dataclass(frozen=True)
class TokenDef:
    symbol: str          # canonical symbol (CEX-comparable, e.g. "BTC" for WBTC)
    address: str
    decimals: int


# ── Verified ERC-20/BEP-20 token sets per network (§3.4 allowlist, data input) ──
ETHEREUM_TOKENS: list[TokenDef] = [
    TokenDef("ETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),   # WETH
    TokenDef("BTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),    # WBTC
    TokenDef("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA", 18),
    TokenDef("UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", 18),
    TokenDef("AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", 18),
    TokenDef("PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18),
    TokenDef("SHIB", "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", 18),
    TokenDef("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18),
    TokenDef("MKR", "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2", 18),
    TokenDef("LDO", "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", 18),
]

BNB_TOKENS: list[TokenDef] = [
    TokenDef("BNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18),   # WBNB
    TokenDef("ETH", "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18),
    TokenDef("BTC", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", 18),   # BTCB
    TokenDef("CAKE", "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18),
    TokenDef("XRP", "0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE", 18),
    TokenDef("ADA", "0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47", 18),
    TokenDef("DOGE", "0xbA2aE424d960c26247Dd6c32edC70B295c744C43", 8),
    TokenDef("DOT", "0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402", 18),
    TokenDef("LTC", "0x4338665CBB7B2485A8855A139b75D5e34AB0DB94", 18),
    TokenDef("MATIC", "0xCC42724C6683B7E57334c4E856f4c9965ED682bD", 18),
]

# USDT only (§3.6). Dropping the USDC quote halves on-chain pool discovery: v2/v3
# discovery enumerates base × quote, so every base now resolves one pool per fee tier
# instead of two.
QUOTE_TOKENS: dict[str, list[TokenDef]] = {
    "ethereum": [
        TokenDef("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    ],
    "bnb": [
        TokenDef("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
    ],
}

BASE_TOKENS: dict[str, list[TokenDef]] = {
    "ethereum": ETHEREUM_TOKENS,
    "bnb": BNB_TOKENS,
}

# V2 factory addresses (getPair discovery).
UNISWAP_V2_FACTORY: dict[str, str] = {
    "ethereum": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
}
SUSHISWAP_V2_FACTORY: dict[str, str] = {
    "ethereum": "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",
}
PANCAKE_V2_FACTORY: dict[str, str] = {
    "bnb": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
}

UNISWAP_FEE = Decimal("0.003")
SUSHI_FEE = Decimal("0.003")
PANCAKE_FEE = Decimal("0.0025")

# V3 factory addresses (getPool(token0, token1, feeTier) discovery) + fee tiers (in
# hundredths of a bip, the on-chain uint24). Each tier is a distinct pool.
UNISWAP_V3_FACTORY: dict[str, str] = {
    "ethereum": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
}
UNISWAP_V3_FEE_TIERS: tuple[int, ...] = (500, 3000, 10000)
PANCAKE_V3_FACTORY: dict[str, str] = {
    "bnb": "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865",
}
PANCAKE_V3_FEE_TIERS: tuple[int, ...] = (100, 500, 2500, 10000)


# ── Curated cold-start fallback pools (used until on-chain discovery succeeds) ──
UNISWAP_POOLS: dict[str, list[PoolDef]] = {
    "ethereum": [
        # Uniswap V2 WETH/USDT
        PoolDef("ETH", "USDT", "0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
                token0_is_base=True, base_decimals=18, quote_decimals=6, fee_tier=UNISWAP_FEE),
    ],
}

PANCAKE_POOLS: dict[str, list[PoolDef]] = {
    "bnb": [
        # PancakeSwap V2 WBNB/USDT
        PoolDef("BNB", "USDT", "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE",
                token0_is_base=True, base_decimals=18, quote_decimals=18, fee_tier=PANCAKE_FEE),
    ],
}

# SushiSwap's only curated pool was USDC/WETH, removed with the USDC quote (§3.6). No
# verified USDT replacement address is pinned here, so this venue has no cold-start
# fallback and stays empty until on-chain v2 discovery resolves its USDT pools.
SUSHI_POOLS: dict[str, list[PoolDef]] = {
    "ethereum": [],
}

# Verified base assets eligible for DEX/cross-chain scanning (§3.4 allowlist).
# Union of every TokenDef symbol above + the Solana (Jupiter) verified set.
VERIFIED_TOKENS: set[str] = (
    {t.symbol for t in ETHEREUM_TOKENS} | {t.symbol for t in BNB_TOKENS}
    | {"SOL", "USDT", "ARB", "OP",
       "JUP", "BONK", "WIF", "JTO", "PYTH", "RAY", "ORCA", "WLD", "RENDER"}
)
