"""
Decodificador manual de wire-format protobuf (formato binário TLV: tag,
wire-type, comprimento, valor) para o wrapper `PushDataV3ApiWrapper` da MEXC.

Por que não usar um .proto compilado para o wrapper inteiro: a estrutura
exata do `oneof body` (qual número de campo corresponde a
`publicBookTicker`) não pôde ser confirmada com confiança suficiente por
fontes independentes concordantes - um relato técnico confiável reporta que
a documentação oficial de .proto da MEXC está desatualizada/incorreta nesse
ponto. Usar um número de campo errado faria o parser falhar silenciosamente
(o pior cenário: preços errados sem nenhum erro visível).

Em vez disso, este decodificador faz a leitura genérica do wire-format
(sem assumir nomes/números de campo do `oneof`), extrai os campos
top-level conhecidos por tipo (strings simples: channel, symbol), e trata
qualquer campo do tipo "length-delimited" (wire-type 2) não identificado
como candidato a ser o body aninhado (a mensagem PublicBookTickerV3Api).
Esse candidato é então validado (deve decodificar como
PublicBookTickerV3Api com bidPrice/askPrice numéricos e positivos) antes
de ser aceito - qualquer coisa que não bata com esse formato é descartada,
nunca usada. Essa validação é a proteção real contra decodificar dados
errados sem perceber.
"""
import logging

from bot.proto import PublicBookTickerV3Api_pb2

logger = logging.getLogger("mexc_protobuf_decoder")

WIRE_TYPE_VARINT = 0
WIRE_TYPE_FIXED64 = 1
WIRE_TYPE_LENGTH_DELIMITED = 2
WIRE_TYPE_FIXED32 = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Lê um varint a partir de `pos`. Retorna (valor, nova_posicao)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Varint truncado (fim do buffer inesperado)")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift >= 64:
            raise ValueError("Varint excede 64 bits (dado corrompido ou formato inesperado)")
    return result, pos


def decode_wrapper_fields(buf: bytes) -> dict:
    """
    Faz uma varredura genérica do wire-format, retornando um dicionário
    {numero_do_campo: valor_bruto} para todos os campos top-level.
    Campos length-delimited (strings ou submensagens) ficam como bytes;
    campos varint ficam como int. Não assume nenhum schema.
    """
    fields: dict[int, list] = {}
    pos = 0
    n = len(buf)

    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == WIRE_TYPE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == WIRE_TYPE_LENGTH_DELIMITED:
            length, pos = _read_varint(buf, pos)
            if pos + length > n:
                raise ValueError("Campo length-delimited excede o tamanho do buffer")
            value = buf[pos:pos + length]
            pos += length
        elif wire_type == WIRE_TYPE_FIXED64:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == WIRE_TYPE_FIXED32:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"Wire type desconhecido: {wire_type}")

        fields.setdefault(field_number, []).append(value)

    return fields


def _try_decode_book_ticker(raw: bytes) -> dict | None:
    """
    Tenta decodificar `raw` como PublicBookTickerV3Api e valida o
    resultado. Só retorna algo se os 4 campos existirem e os preços forem
    numéricos e positivos - essa validação é o que impede aceitar bytes
    que por acaso "parseiam sem erro" mas não são realmente um bookTicker.
    """
    try:
        msg = PublicBookTickerV3Api_pb2.PublicBookTickerV3Api()
        msg.ParseFromString(raw)
    except Exception:
        return None

    try:
        bid_price = float(msg.bidPrice)
        ask_price = float(msg.askPrice)
        bid_qty = float(msg.bidQuantity)
        ask_qty = float(msg.askQuantity)
    except (ValueError, TypeError):
        return None

    if bid_price <= 0 or ask_price <= 0 or bid_qty < 0 or ask_qty < 0:
        return None
    if ask_price < bid_price:
        # Livro invertido não é fisicamente válido (ask sempre >= bid num
        # book saudável) - sinal de que decodificamos a coisa errada.
        return None

    return {
        "bid_price": bid_price,
        "bid_qty": bid_qty,
        "ask_price": ask_price,
        "ask_qty": ask_qty,
    }


def decode_book_ticker_push(buf: bytes) -> dict | None:
    """
    Decodifica uma mensagem push do canal spot@public.bookTicker,
    retornando {"symbol": str, "bid_price": float, "ask_price": float,
    "bid_qty": float, "ask_qty": float} ou None se não for possível
    decodificar/validar com confiança.
    """
    try:
        fields = decode_wrapper_fields(buf)
    except Exception as e:
        logger.debug("Falha ao decodificar wire-format do wrapper: %s", e)
        return None

    symbol = None
    book_ticker = None

    for field_number, values in fields.items():
        for value in values:
            if not isinstance(value, (bytes, bytearray)):
                continue

            # Tenta interpretar como string simples (symbol, channel, etc.)
            # Heurística: strings de symbol são curtas, ASCII, maiúsculas.
            try:
                as_text = value.decode("utf-8")
                if as_text.isascii() and 3 <= len(as_text) <= 30 and as_text.isupper():
                    if symbol is None:
                        symbol = as_text
                    continue
            except UnicodeDecodeError:
                pass

            # Tenta como submensagem PublicBookTickerV3Api
            if book_ticker is None:
                candidate = _try_decode_book_ticker(bytes(value))
                if candidate:
                    book_ticker = candidate

    if book_ticker is None:
        return None

    result = dict(book_ticker)
    result["symbol"] = symbol
    return result
