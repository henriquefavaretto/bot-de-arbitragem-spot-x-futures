"""
Persistência dos extremos e cruzamentos por COMBINAÇÃO.

## Por que não reaproveitar o storage.py

O `storage.py` guarda extremos por `futures_symbol` — um registro por par,
porque no mundo MEXC-only existia exatamente uma combinação por símbolo.
Aqui existem até 12 por símbolo (~5900 no total), e cada uma tem os próprios
mínimos e máximos: o spread MEXC spot × MEXC futures de JIMOTHY não tem
relação nenhuma com o spread Gate spot × BingX futures do mesmo JIMOTHY.

## Por que o histórico do gráfico NÃO fica aqui

O `storage.py` grava uma amostra de spread por par a cada ciclo. Com 5900
combinações a cada 5 segundos isso daria ~1180 escritas por segundo — o
mesmo antipadrão que já custou 4,82s por ciclo neste projeto (bug 9), agora
multiplicado por dez.

A sparkline é acumulada no NAVEGADOR, a partir dos snapshots que ele já
recebe (é assim que a aba Dashboard funciona). O que fica no banco é só o que
precisa sobreviver a um reload: os extremos e a contagem de cruzamentos —
uma linha por combinação, escrita em lote.
"""
import asyncio
import logging
import time
from typing import Optional

import aiosqlite

logger = logging.getLogger("multi_storage")

DB_PATH = "arb_multi.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS combo_extremes (
    combo_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    buy_venue TEXT NOT NULL,
    sell_venue TEXT NOT NULL,
    min_entry_pct REAL, min_entry_ts REAL,
    max_entry_pct REAL, max_entry_ts REAL,
    min_exit_pct REAL, min_exit_ts REAL,
    max_exit_pct REAL, max_exit_ts REAL,
    crossings INTEGER NOT NULL DEFAULT 0,
    last_crossing_ts REAL,
    updated_ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_combo_extremes_symbol ON combo_extremes (symbol);
"""


class MultiStorage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def load_all(self) -> dict[str, dict]:
        """Carrega todos os extremos de uma vez, na inicialização."""
        if self._db is None:
            return {}
        cur = await self._db.execute(
            "SELECT combo_key, min_entry_pct, min_entry_ts, max_entry_pct, max_entry_ts, "
            "min_exit_pct, min_exit_ts, max_exit_pct, max_exit_ts, crossings, last_crossing_ts "
            "FROM combo_extremes"
        )
        rows = await cur.fetchall()
        return {
            r[0]: {
                "min_entry_pct": r[1], "min_entry_ts": r[2],
                "max_entry_pct": r[3], "max_entry_ts": r[4],
                "min_exit_pct": r[5], "min_exit_ts": r[6],
                "max_exit_pct": r[7], "max_exit_ts": r[8],
                "crossings": r[9] or 0, "last_crossing_ts": r[10],
            }
            for r in rows
        }

    async def save_batch(self, registros: list[tuple]):
        """
        Grava um lote inteiro num único commit.

        Um commit por combinação forçaria um fsync em disco por linha; com
        milhares de combinações por ciclo isso dominaria o tempo do loop
        inteiro. É a mesma lição do bug 9 (ganho de 26,7x ao agrupar), e o
        motivo de esta ser a única forma de escrita exposta pela classe.
        """
        if self._db is None or not registros:
            return
        async with self._lock:
            await self._db.executemany(
                """
                INSERT INTO combo_extremes
                    (combo_key, symbol, buy_venue, sell_venue,
                     min_entry_pct, min_entry_ts, max_entry_pct, max_entry_ts,
                     min_exit_pct, min_exit_ts, max_exit_pct, max_exit_ts,
                     crossings, last_crossing_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(combo_key) DO UPDATE SET
                    min_entry_pct = excluded.min_entry_pct,
                    min_entry_ts = excluded.min_entry_ts,
                    max_entry_pct = excluded.max_entry_pct,
                    max_entry_ts = excluded.max_entry_ts,
                    min_exit_pct = excluded.min_exit_pct,
                    min_exit_ts = excluded.min_exit_ts,
                    max_exit_pct = excluded.max_exit_pct,
                    max_exit_ts = excluded.max_exit_ts,
                    crossings = excluded.crossings,
                    last_crossing_ts = excluded.last_crossing_ts,
                    updated_ts = excluded.updated_ts
                """,
                registros,
            )
            await self._db.commit()

    async def clear(self, symbol: Optional[str] = None) -> int:
        if self._db is None:
            return 0
        async with self._lock:
            if symbol:
                cur = await self._db.execute(
                    "SELECT COUNT(*) FROM combo_extremes WHERE symbol = ?", (symbol,))
                n = (await cur.fetchone())[0]
                await self._db.execute("DELETE FROM combo_extremes WHERE symbol = ?", (symbol,))
            else:
                cur = await self._db.execute("SELECT COUNT(*) FROM combo_extremes")
                n = (await cur.fetchone())[0]
                await self._db.execute("DELETE FROM combo_extremes")
            await self._db.commit()
        return n
