"""Conjuntos de ícones da interface.

O app nasceu com emoji no lugar de ícone (🎤 ⏸ 🗑 …). Aqui ficam os pacotes
alternativos: cada um é uma pasta em `icones/` com um arquivo por *slot*, e o
slot é o nome do que o ícone faz — não do desenho. Assim trocar de pacote é
trocar de pasta, e um desenho novo entra sem mexer em quem usa.

Quem escolhe é a configuração (`icones`), igual ao tema. O padrão continua
sendo emoji: nada muda para quem não mexer.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

log = logging.getLogger(__name__)

PASTA = Path(__file__).parent / "icones"

# slot -> emoji que ele usava (é o que aparece no pacote "emoji")
SLOTS = {
    "enviar": "➤",
    "microfone": "🎤",
    "play": "▶",
    "pausar": "⏸",
    "lixeira": "🗑",
    "pino": "📌",
    "sino_cortado": "🔕",
    "arquivar": "🗄",
    "estrela": "★",
    "responder": "↩",
    "encaminhar": "➦",
    "busca": "🔍",
    "config": "⚙",
    "fechar": "✕",
    "papel": "📎",
    "camera": "📷",
    "imagem": "🖼",
    "video": "🎬",
    "documento": "📄",
    "mapa": "📍",
    "contato": "👤",
    "cadeado": "🔒",
    "aviso": "⚠",
    "relogio": "🕐",
    "mais": "＋",
}

# pacote -> rótulo mostrado na configuração
PACOTES = {
    "emoji": "Emoji (padrão)",
    "fluent-phosphor": "Fluent Color + Phosphor",
    "icons8-color": "Icons8 Color",
    "icons8-fluency": "Icons8 Fluency",
}
PADRAO = "emoji"

# pacotes que pedem crédito visível (licença da Icons8)
CREDITOS = {
    "icons8-color": "Ícones: Icons8 (icons8.com)",
    "icons8-fluency": "Ícones: Icons8 (icons8.com)",
}


def normalizar(pacote: str | None) -> str:
    return pacote if pacote in PACOTES else PADRAO


def arquivo(pacote: str, slot: str) -> Path | None:
    """Caminho do desenho do slot naquele pacote, ou None se for emoji."""
    if normalizar(pacote) == PADRAO:
        return None
    pasta = PASTA / normalizar(pacote)
    for ext in (".svg", ".png"):
        caminho = pasta / f"{slot}{ext}"
        if caminho.exists():
            return caminho
    return None


def emoji(slot: str) -> str:
    return SLOTS.get(slot, "")


@lru_cache(maxsize=512)
def _pixmap(caminho: str, px: int) -> QPixmap:
    return QPixmap(caminho).scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)


@lru_cache(maxsize=512)
def _icone(caminho: str, px: int) -> QIcon:
    return QIcon(_pixmap(caminho, px))


def icone(pacote: str, slot: str, px: int = 22) -> QIcon | None:
    """QIcon do slot, ou None quando o pacote é emoji (aí use `emoji()`)."""
    caminho = arquivo(pacote, slot)
    if caminho is None:
        return None
    try:
        return _icone(str(caminho), px)
    except Exception:  # arquivo corrompido não pode derrubar a janela
        log.warning("ícone ilegível: %s", caminho)
        return None


def limpar_cache():
    _pixmap.cache_clear()
    _icone.cache_clear()
