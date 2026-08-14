"""
Camada de persistência (SQLite) para:
- Contadores de cruzamento por par (persistem entre reinícios do backend)
- Histórico de spread (para sparkline e para detectar cruzamentos)
"""
import aiosqlite
import time
from typing import Optional

from config import DB_PATH, SPREAD_HISTORY_MAX_POINTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS crossings (
    symbol TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    last_crossing_ts REAL,
    last_sign INTEGER  -- -1, 0 ou 1: sinal do spread na última leitura
);

CREATE TABLE IF NOT EXISTS crossing_events (
    symbol TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crossing_events_symbol_ts
    ON crossing_events (symbol, ts);

CREATE TABLE IF NOT EXISTS spread_history (
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    spread_pct REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spread_history_symbol_ts
    ON spread_history (symbol, ts);

CREATE TABLE IF NOT EXISTS spread_extremes (
    symbol TEXT PRIMARY KEY,
    min_spread_pct REAL NOT NULL,
    min_spread_ts REAL NOT NULL,
    max_spread_pct REAL NOT NULL,
    max_spread_ts REAL NOT NULL
);

-- Extremos do spread de SAÍDA (fut_ask vs spot_bid), separados dos de
-- entrada porque são grandezas diferentes: a saída executa nos lados
-- opostos do book e seu spread é sistematicamente maior.
CREATE TABLE IF NOT EXISTS exit_spread_extremes (
    symbol TEXT PRIMARY KEY,
    min_spread_pct REAL NOT NULL,
    min_spread_ts REAL NOT NULL,
    max_spread_pct REAL NOT NULL,
    max_spread_ts REAL NOT NULL
);
"""

# Tabelas de extremos permitidas. O nome da tabela é interpolado direto na
# query (SQLite não aceita parâmetro para nome de tabela), então precisa ser
# validado contra esta lista - nunca aceitar string arbitrária vinda de fora.
EXTREMES_TABLES = ("spread_extremes", "exit_spread_extremes")


def _validate_extremes_table(table: str) -> str:
    if table not in EXTREMES_TABLES:
        raise ValueError(f"Tabela de extremos inválida: {table!r}")
    return table


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # ---------------- Crossings ----------------

    async def get_all_crossings(self) -> dict:
        """Retorna {symbol: {"count": int, "last_crossing_ts": float|None, "last_sign": int|None}}"""
        cur = await self._db.execute(
            "SELECT symbol, count, last_crossing_ts, last_sign FROM crossings"
        )
        rows = await cur.fetchall()
        return {
            row[0]: {"count": row[1], "last_crossing_ts": row[2], "last_sign": row[3]}
            for row in rows
        }

    async def register_spread_sample(self, symbol: str, spread_pct: float, defer_commit: bool = False) -> dict:
        """
        Registra uma nova amostra de spread para o par, detecta se houve
        cruzamento de sinal desde a última amostra, e atualiza o contador.

        Retorna o estado atualizado: {"count", "last_crossing_ts", "crossed": bool}
        """
        sign = 1 if spread_pct > 0 else (-1 if spread_pct < 0 else 0)
        now = time.time()

        cur = await self._db.execute(
            "SELECT count, last_sign, last_crossing_ts FROM crossings WHERE symbol = ?", (symbol,)
        )
        row = await cur.fetchone()

        crossed = False
        last_crossing_ts_existing = None
        if row is None:
            count, last_sign = 0, sign
            await self._db.execute(
                "INSERT INTO crossings (symbol, count, last_crossing_ts, last_sign) VALUES (?, ?, NULL, ?)",
                (symbol, count, sign),
            )
        else:
            count, last_sign, last_crossing_ts_existing = row
            # Cruzamento = mudança real de sinal (ignora transições envolvendo 0)
            if last_sign != 0 and sign != 0 and last_sign != sign:
                crossed = True
                count += 1
                await self._db.execute(
                    "UPDATE crossings SET count = ?, last_crossing_ts = ?, last_sign = ? WHERE symbol = ?",
                    (count, now, sign, symbol),
                )
                await self._db.execute(
                    "INSERT INTO crossing_events (symbol, ts) VALUES (?, ?)",
                    (symbol, now),
                )
            else:
                # Atualiza apenas o sinal (se não era 0) sem contar cruzamento
                new_sign = sign if sign != 0 else last_sign
                await self._db.execute(
                    "UPDATE crossings SET last_sign = ? WHERE symbol = ?",
                    (new_sign, symbol),
                )

        # Histórico de spread (para sparkline)
        await self._db.execute(
            "INSERT INTO spread_history (symbol, ts, spread_pct) VALUES (?, ?, ?)",
            (symbol, now, spread_pct),
        )
        if not defer_commit:
            await self._db.commit()

        # Retorna os valores já calculados acima, sem reler do banco - a
        # releitura era uma query extra por par a cada ciclo, desperdício
        # relevante quando há centenas de pares monitorados.
        return {
            "count": count,
            "last_crossing_ts": now if crossed else last_crossing_ts_existing,
            "crossed": crossed,
        }

    async def commit(self):
        """
        Faz commit das escritas pendentes. Usado em conjunto com
        `defer_commit=True` nos métodos de escrita: em vez de um commit por
        par (caro em SQLite, pois envolve fsync em disco), o chamador agrupa
        centenas de escritas num único commit ao fim do ciclo.
        """
        await self._db.commit()

    async def get_spread_history(self, symbol: str, limit: int = SPREAD_HISTORY_MAX_POINTS) -> list:
        cur = await self._db.execute(
            "SELECT ts, spread_pct FROM spread_history WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        )
        rows = await cur.fetchall()
        rows.reverse()
        return [{"ts": r[0], "spread_pct": r[1]} for r in rows]

    async def get_crossing_counts_since(self, since_ts: float) -> dict:
        """
        Retorna {symbol: count} com a quantidade de cruzamentos de cada par
        que ocorreram desde `since_ts` (timestamp unix). Uma única query para
        todos os pares, usada para as janelas 1h / 12h / 24h.
        """
        cur = await self._db.execute(
            "SELECT symbol, COUNT(*) FROM crossing_events WHERE ts >= ? GROUP BY symbol",
            (since_ts,),
        )
        rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def prune_crossing_events(self, max_age_seconds: float = 48 * 3600):
        """Remove eventos de cruzamento mais antigos que max_age_seconds (mantém margem acima de 24h)."""
        cutoff = time.time() - max_age_seconds
        await self._db.execute("DELETE FROM crossing_events WHERE ts < ?", (cutoff,))
        await self._db.commit()

    # ---------------- Extremos de spread (min/max histórico) ----------------

    async def update_spread_extremes(
        self, symbol: str, spread_pct: float, defer_commit: bool = False,
        table: str = "spread_extremes",
    ) -> dict:
        """
        Atualiza (se necessário) o menor e o maior spread já vistos para o par,
        de forma incremental (O(1), sem precisar guardar o histórico completo).
        Persiste em SQLite, então sobrevive a reinícios do backend.

        Retorna o estado atualizado:
            {"min_spread_pct", "min_spread_ts", "max_spread_pct", "max_spread_ts"}
        """
        _validate_extremes_table(table)
        now = time.time()
        cur = await self._db.execute(
            "SELECT min_spread_pct, min_spread_ts, max_spread_pct, max_spread_ts "
            f"FROM {table} WHERE symbol = ?",
            (symbol,),
        )
        row = await cur.fetchone()

        if row is None:
            min_v, min_ts, max_v, max_ts = spread_pct, now, spread_pct, now
            await self._db.execute(
                f"INSERT INTO {table} "
                "(symbol, min_spread_pct, min_spread_ts, max_spread_pct, max_spread_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, min_v, min_ts, max_v, max_ts),
            )
        else:
            min_v, min_ts, max_v, max_ts = row
            changed = False
            if spread_pct < min_v:
                min_v, min_ts = spread_pct, now
                changed = True
            if spread_pct > max_v:
                max_v, max_ts = spread_pct, now
                changed = True
            if changed:
                await self._db.execute(
                    f"UPDATE {table} SET "
                    "min_spread_pct = ?, min_spread_ts = ?, max_spread_pct = ?, max_spread_ts = ? "
                    "WHERE symbol = ?",
                    (min_v, min_ts, max_v, max_ts, symbol),
                )

        if not defer_commit:
            await self._db.commit()
        return {
            "min_spread_pct": min_v,
            "min_spread_ts": min_ts,
            "max_spread_pct": max_v,
            "max_spread_ts": max_ts,
        }

    async def get_all_spread_extremes(self, table: str = "spread_extremes") -> dict:
        """Retorna {symbol: {min_spread_pct, min_spread_ts, max_spread_pct, max_spread_ts}} para todos os pares."""
        _validate_extremes_table(table)
        cur = await self._db.execute(
            f"SELECT symbol, min_spread_pct, min_spread_ts, max_spread_pct, max_spread_ts FROM {table}"
        )
        rows = await cur.fetchall()
        return {
            row[0]: {
                "min_spread_pct": row[1],
                "min_spread_ts": row[2],
                "max_spread_pct": row[3],
                "max_spread_ts": row[4],
            }
            for row in rows
        }

    async def clear_spread_extremes(self, symbol: str | None = None, table: str = "spread_extremes") -> int:
        """
        Apaga os extremos (mín/máx histórico) de spread. Sem `symbol`, apaga
        de todos os pares. Útil para descartar recordes que foram registrados
        a partir de preços não-executáveis (antes da correção que passou a
        exigir preços do book). Retorna quantas linhas foram removidas.
        """
        _validate_extremes_table(table)
        if symbol:
            cur = await self._db.execute(f"SELECT COUNT(*) FROM {table} WHERE symbol = ?", (symbol,))
            count = (await cur.fetchone())[0]
            await self._db.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
        else:
            cur = await self._db.execute(f"SELECT COUNT(*) FROM {table}")
            count = (await cur.fetchone())[0]
            await self._db.execute(f"DELETE FROM {table}")
        await self._db.commit()
        return count

    async def prune_history(self, keep_per_symbol: int = SPREAD_HISTORY_MAX_POINTS):
        """Remove pontos antigos de histórico além do limite, por símbolo, para não crescer indefinidamente."""
        await self._db.execute(
            """
            DELETE FROM spread_history
            WHERE rowid NOT IN (
                SELECT rowid FROM (
                    SELECT rowid,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) as rn
                    FROM spread_history
                ) WHERE rn <= ?
            )
            """,
            (keep_per_symbol,),
        )
        await self._db.commit()
