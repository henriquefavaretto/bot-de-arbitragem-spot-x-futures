"""
Persistência do bot de arbitragem: configuração por par, posições e histórico
de execuções. Banco SQLite separado do dashboard (arb_dashboard.db), para não
misturar dados de monitoramento com dados operacionais do bot.
"""
import aiosqlite
import time
import json
from typing import Optional

BOT_DB_PATH = "arb_bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_pair_config (
    symbol TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    entry_spread_pct REAL NOT NULL,
    exit_spread_pct REAL NOT NULL,
    position_size_usdt REAL NOT NULL,
    updated_ts REAL NOT NULL
);

-- Colunas de venue adicionadas quando o bot deixou de ser MEXC-only.
-- Configs antigas nao tem esses campos; a migracao em `_migrate` preenche
-- com mexc:spot/mexc:futures, que era o unico comportamento possivel antes.
-- Assumir NULL como "qualquer venue" seria perigoso: um par configurado ha
-- semanas passaria a operar numa exchange que o usuario nunca escolheu.

CREATE TABLE IF NOT EXISTS bot_positions (
    symbol TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    entry_spread_pct REAL,
    entry_spot_price REAL,
    entry_futures_price REAL,
    entry_spot_qty REAL,
    entry_futures_vol REAL,
    entry_notional_usdt REAL,
    entry_ts REAL,
    simulated INTEGER NOT NULL DEFAULT 1,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 1,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_trade_log_symbol_ts ON bot_trade_log (symbol, ts);
"""


class BotStorage:
    def __init__(self, db_path: str = BOT_DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self):
        """
        Acrescenta as colunas de venue em bancos criados antes do bot ser
        multi-exchange.

        O padrao e mexc:spot/mexc:futures porque era literalmente o unico
        comportamento possivel antes: um par ja configurado continua fazendo
        exatamente o que fazia. Deixar NULL e interpretar como "qualquer
        venue" faria um par antigo comecar a operar numa exchange que o
        usuario nunca escolheu -- o tipo de mudanca silenciosa de
        comportamento que este projeto nao aceita em codigo que move dinheiro.
        """
        cur = await self._db.execute("PRAGMA table_info(bot_pair_config)")
        colunas = {row[1] for row in await cur.fetchall()}
        if "buy_venue" not in colunas:
            await self._db.execute(
                "ALTER TABLE bot_pair_config ADD COLUMN buy_venue TEXT NOT NULL DEFAULT 'mexc:spot'"
            )
        if "sell_venue" not in colunas:
            await self._db.execute(
                "ALTER TABLE bot_pair_config ADD COLUMN sell_venue TEXT NOT NULL DEFAULT 'mexc:futures'"
            )

    async def close(self):
        if self._db:
            await self._db.close()

    # ---------------- Configuração por par ----------------

    async def upsert_pair_config(
        self, symbol: str, enabled: bool, entry_spread_pct: float,
        exit_spread_pct: float, position_size_usdt: float,
        buy_venue: str = "mexc:spot", sell_venue: str = "mexc:futures",
    ):
        await self._db.execute(
            """
            INSERT INTO bot_pair_config
                (symbol, enabled, entry_spread_pct, exit_spread_pct, position_size_usdt,
                 buy_venue, sell_venue, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                enabled = excluded.enabled,
                entry_spread_pct = excluded.entry_spread_pct,
                exit_spread_pct = excluded.exit_spread_pct,
                position_size_usdt = excluded.position_size_usdt,
                buy_venue = excluded.buy_venue,
                sell_venue = excluded.sell_venue,
                updated_ts = excluded.updated_ts
            """,
            (symbol, int(enabled), entry_spread_pct, exit_spread_pct, position_size_usdt,
             buy_venue, sell_venue, time.time()),
        )
        await self._db.commit()

    async def get_all_pair_configs(self) -> dict:
        cur = await self._db.execute(
            "SELECT symbol, enabled, entry_spread_pct, exit_spread_pct, position_size_usdt, "
            "buy_venue, sell_venue FROM bot_pair_config"
        )
        rows = await cur.fetchall()
        return {
            row[0]: {
                "enabled": bool(row[1]),
                "entry_spread_pct": row[2],
                "exit_spread_pct": row[3],
                "position_size_usdt": row[4],
                "buy_venue": row[5] or "mexc:spot",
                "sell_venue": row[6] or "mexc:futures",
            }
            for row in rows
        }

    async def delete_pair_config(self, symbol: str):
        await self._db.execute("DELETE FROM bot_pair_config WHERE symbol = ?", (symbol,))
        await self._db.commit()

    # ---------------- Posições ----------------

    async def upsert_position(self, symbol: str, state: str, simulated: bool, **fields):
        existing = await self.get_position(symbol)
        merged = {**(existing or {}), **fields, "state": state, "simulated": simulated}

        await self._db.execute(
            """
            INSERT INTO bot_positions
                (symbol, state, entry_spread_pct, entry_spot_price, entry_futures_price,
                 entry_spot_qty, entry_futures_vol, entry_notional_usdt, entry_ts, simulated, updated_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                state = excluded.state,
                entry_spread_pct = excluded.entry_spread_pct,
                entry_spot_price = excluded.entry_spot_price,
                entry_futures_price = excluded.entry_futures_price,
                entry_spot_qty = excluded.entry_spot_qty,
                entry_futures_vol = excluded.entry_futures_vol,
                entry_notional_usdt = excluded.entry_notional_usdt,
                entry_ts = excluded.entry_ts,
                simulated = excluded.simulated,
                updated_ts = excluded.updated_ts
            """,
            (
                symbol, state,
                merged.get("entry_spread_pct"), merged.get("entry_spot_price"), merged.get("entry_futures_price"),
                merged.get("entry_spot_qty"), merged.get("entry_futures_vol"), merged.get("entry_notional_usdt"),
                merged.get("entry_ts"), int(simulated), time.time(),
            ),
        )
        await self._db.commit()

    async def get_position(self, symbol: str) -> Optional[dict]:
        cur = await self._db.execute(
            """
            SELECT symbol, state, entry_spread_pct, entry_spot_price, entry_futures_price,
                   entry_spot_qty, entry_futures_vol, entry_notional_usdt, entry_ts, simulated
            FROM bot_positions WHERE symbol = ?
            """,
            (symbol,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "symbol": row[0], "state": row[1], "entry_spread_pct": row[2],
            "entry_spot_price": row[3], "entry_futures_price": row[4],
            "entry_spot_qty": row[5], "entry_futures_vol": row[6],
            "entry_notional_usdt": row[7], "entry_ts": row[8], "simulated": bool(row[9]),
        }

    async def get_all_positions(self) -> dict:
        cur = await self._db.execute(
            """
            SELECT symbol, state, entry_spread_pct, entry_spot_price, entry_futures_price,
                   entry_spot_qty, entry_futures_vol, entry_notional_usdt, entry_ts, simulated
            FROM bot_positions
            """
        )
        rows = await cur.fetchall()
        return {
            row[0]: {
                "symbol": row[0], "state": row[1], "entry_spread_pct": row[2],
                "entry_spot_price": row[3], "entry_futures_price": row[4],
                "entry_spot_qty": row[5], "entry_futures_vol": row[6],
                "entry_notional_usdt": row[7], "entry_ts": row[8], "simulated": bool(row[9]),
            }
            for row in rows
        }

    async def clear_position(self, symbol: str):
        await self._db.execute("DELETE FROM bot_positions WHERE symbol = ?", (symbol,))
        await self._db.commit()

    # ---------------- Log de eventos/trades ----------------

    async def log_event(self, symbol: str, event: str, detail: dict, simulated: bool):
        await self._db.execute(
            "INSERT INTO bot_trade_log (symbol, event, detail_json, simulated, ts) VALUES (?, ?, ?, ?, ?)",
            (symbol, event, json.dumps(detail), int(simulated), time.time()),
        )
        await self._db.commit()

    async def get_recent_events(self, symbol: Optional[str] = None, limit: int = 200) -> list:
        if symbol:
            cur = await self._db.execute(
                "SELECT symbol, event, detail_json, simulated, ts FROM bot_trade_log "
                "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT symbol, event, detail_json, simulated, ts FROM bot_trade_log ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        return [
            {
                "symbol": r[0], "event": r[1], "detail": json.loads(r[2]),
                "simulated": bool(r[3]), "ts": r[4],
            }
            for r in rows
        ]

    async def clear_events(self, symbol: Optional[str] = None) -> int:
        """Apaga o histórico de eventos (todos, ou só de um par). Retorna quantas linhas foram removidas."""
        if symbol:
            cur = await self._db.execute("SELECT COUNT(*) FROM bot_trade_log WHERE symbol = ?", (symbol,))
            count = (await cur.fetchone())[0]
            await self._db.execute("DELETE FROM bot_trade_log WHERE symbol = ?", (symbol,))
        else:
            cur = await self._db.execute("SELECT COUNT(*) FROM bot_trade_log")
            count = (await cur.fetchone())[0]
            await self._db.execute("DELETE FROM bot_trade_log")
        await self._db.commit()
        return count

