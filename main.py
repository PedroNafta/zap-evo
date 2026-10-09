# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "python-socketio[client]", "httpx", "psycopg[binary]"]
# ///
"""Cliente WhatsApp nativo sobre Evolution API — Fase 1.

    uv run main.py

Config em ~/.config/evolution-client/config.toml (criada no primeiro uso).
"""

from __future__ import annotations

import base64
import logging
import math
import os
import pathlib
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlparse
import unicodedata

import shiboken6
import socketio
from PySide6.QtMultimedia import (
    QAudioInput,
    QAudioOutput,
    QCamera,
    QImageCapture,
    QMediaCaptureSession,
    QMediaDevices,
    QMediaFormat,
    QMediaPlayer,
    QMediaRecorder,
    QVideoSink,
)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtDBus import QDBus, QDBusConnection, QDBusInterface
from PySide6.QtGui import (
    QColor,
    QTextCursor,
    QTextDocument,
    QIcon,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QPainter,
    QKeySequence,
    QMovie,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyleOptionViewItem,
    QStyle,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import api
import emojis

log = logging.getLogger("cliente")

TETO = 500  # ponytail: mensagens vivas no modelo. Sem isto, rolar por horas
# reconstroi o vazamento do ZapZap: 50 msgs = 51KB, as 286k seriam ~286MB.
PAGINA = 50
# Varredura periódica do cache de conversas. Entre uma varredura e outra, a
# troca de conversa também limpa — assim o intervalo longo não deixa o heap
# acumular, e a varredura só existe para o caso de o app ficar parado.
ROTACAO_MS = 90 * 60 * 1000
# nome por JID, para o balão resolver autor de citação sem carregar o modelo
CONTATOS: dict[str, str] = {}
# O Web escreve "Você" quando a mensagem CITADA é minha. O `contextInfo.
# participant` NÃO serve para isso: medido em conversa 1:1, ele traz o jid da
# CONVERSA, não de quem gravou. O Web acerta porque resolve a citada pelo
# `stanzaId` na base local dele e lê o `fromMe` de lá — é o que fazemos aqui.
EU_JIDS: set[str] = {"eu"}
MINHAS: set[str] = set()  # ids das minhas mensagens vistas nesta sessao


def aprender_eu(registros: list[dict]):
    """Índice do que é meu: ids (para a citação) e jids (para grupo).

    ponytail: um set de ids no lugar de consultar o banco a cada citação. Cabe
    em ~2 MB para 20 mil mensagens e zera se passar disso — a conversa aberta
    repovoa em uma passada.
    """
    if len(MINHAS) > 20_000:
        MINHAS.clear()
    for m in registros:
        k = m.get("key") or {}
        if not k.get("fromMe"):
            continue
        if mid := k.get("id"):
            MINHAS.add(mid)
        if quem := k.get("participant"):
            EU_JIDS.add(quem)
# ids de imagens que ENVIAMOS em HD — marca local: a Evolution 2.3.7 não grava
# o midQualityFileSha256 do payload (testado); sem isto o selo sumiria ao
# recarregar a conversa
HD_IDS: set[str] = api.carregar_hd_ids()
AUDIO: AudioInline | None = None  # player inline, criado no arranque
POOL = ThreadPoolExecutor(max_workers=4)
# ponytail: midia em pool proprio. Ponteiro expirado leva o timeout inteiro, e
# 4 downloads travados nao podem impedir a conversa de abrir.
MIDIA = ThreadPoolExecutor(max_workers=3)


def rss_mb() -> int:
    campo = open(f"/proc/{os.getpid()}/status").read().split("VmRSS:")[1]
    return int(campo.split()[0]) // 1024


def hora(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# --------------------------------------------------------------------------- modelos


class ChatList(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.itens: list[dict] = []
        self.contatos: dict[str, str] = {}
        self.carregando: dict[str, tuple[int, int, str]] = {}

    def rowCount(self, _=QModelIndex()):
        return len(self.itens)

    def data(self, ix, role=Qt.DisplayRole):
        c = self.itens[ix.row()]
        if role == Qt.DisplayRole:
            return api.nome_do_chat(c, self.contatos)
        if role == Qt.UserRole:
            return c
        if role == Qt.UserRole + 1:
            for j in c.get("_jids") or [c.get("remoteJid")]:
                if j in self.carregando:
                    return self.carregando[j]
            return None
        if role == Qt.ToolTipRole:
            return api.text_of(c.get("lastMessage") or {})[:120]

    def definir(self, chats: list[dict]):
        self.beginResetModel()
        self.itens = chats
        self.endResetModel()

    def mudou(self, chat: dict):
        for i, c in enumerate(self.itens):
            if c is chat:
                ix = self.index(i)
                self.dataChanged.emit(ix, ix)
                return


class Mensagens(QAbstractListModel):
    """Ordem ASC (velho em cima). A API devolve DESC, então invertemos ao inserir."""

    def __init__(self):
        super().__init__()
        self.itens: list[dict] = []
        self.jid = ""
        self.jids: list[str] = []
        self.pagina = 0
        self.paginas = 1

    def rowCount(self, _=QModelIndex()):
        return len(self.itens)

    def data(self, ix, role=Qt.DisplayRole):
        if role == Qt.UserRole:
            return self.itens[ix.row()]
        if role == Qt.DisplayRole:
            return api.text_of(self.itens[ix.row()])

    def abrir(self, jid: str, registros: list[dict], paginas: int):
        self.beginResetModel()
        self.jid, self.paginas = jid, paginas
        # a janela recente pode ter consumido várias páginas; a próxima
        # rolagem para trás continua de onde ela parou
        self.pagina = max(1, -(-len(registros) // PAGINA))
        aprender_eu(registros)
        self.itens = list(reversed(registros))
        self.endResetModel()

    def restaurar(self, jid: str, guardada: dict):
        self.beginResetModel()
        self.jid = jid
        self.jids = guardada["jids"]
        self.itens = guardada["itens"]
        self.pagina = guardada["pagina"]
        self.paginas = guardada["paginas"]
        self.endResetModel()

    def anteriores(self, registros: list[dict]):
        """Insere uma página mais antiga no topo, sem repetir o que já está lá.

        A paginação da API é por offset: se chega mensagem nova entre uma página
        e outra, tudo desloca e a página seguinte devolve linhas que já vieram.
        Sem esta checagem, rolar para cima durante uma conversa ativa duplica
        bolhas na tela.
        """
        aprender_eu(registros)
        ja_tem = {(m.get("key") or {}).get("id") for m in self.itens}
        novos = [m for m in registros if (m.get("key") or {}).get("id") not in ja_tem]
        self.pagina += 1
        if not novos:
            return
        self.beginInsertRows(QModelIndex(), 0, len(novos) - 1)
        self.itens[0:0] = list(reversed(novos))
        self.endInsertRows()
        self._podar(do_fim=True)

    def chegou(self, msg: dict):
        aprender_eu([msg])
        novo = (msg.get("key") or {}).get("id")
        for m in self.itens[-40:]:
            if (m.get("key") or {}).get("id") == novo:
                return  # ja' esta na tela (eco local que virou confirmado)
        self.beginInsertRows(QModelIndex(), len(self.itens), len(self.itens))
        self.itens.append(msg)
        self.endInsertRows()
        self._podar(do_fim=False)

    def mudou(self, msg: dict):
        for i, m in enumerate(self.itens):
            if m is msg:
                ix = self.index(i)
                self.dataChanged.emit(ix, ix)
                return

    def remover(self, msg: dict):
        for i, m in enumerate(self.itens):
            if m is msg:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self.itens[i]
                self.endRemoveRows()
                return

    def _podar(self, do_fim: bool):
        excedente = len(self.itens) - TETO
        if excedente <= 0:
            return
        if do_fim:
            self.beginRemoveRows(QModelIndex(), len(self.itens) - excedente, len(self.itens) - 1)
            del self.itens[-excedente:]
        else:
            self.beginRemoveRows(QModelIndex(), 0, excedente - 1)
            del self.itens[:excedente]
        self.endRemoveRows()


# --------------------------------------------------------------------------- desenho

class Escala:
    """Zoom da interface inteira. Num tiling WM a janela muda de tamanho o tempo
    todo — a fonte precisa acompanhar sem reabrir o app."""

    MIN, MAX, BASE = 10, 26, 14

    def __init__(self):
        self.valor = self.BASE

    def px(self) -> int:
        return self.valor

    def fator(self) -> float:
        return self.valor / self.BASE

    def e(self, n: int) -> int:
        """Escala uma medida fixa (altura de linha, avatar, recuo)."""
        return max(1, round(n * self.fator()))

    def mudar(self, delta: int) -> bool:
        novo = max(self.MIN, min(self.MAX, self.valor + delta))
        if novo == self.valor:
            return False
        self.valor = novo
        return True


ESCALA = Escala()

def _mix(a: str, b: str, t: float) -> str:
    """Mistura dois hex. Serve para derivar estados sem escrever cor à mão."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() * (1 - t) + cb.red() * t),
        round(ca.green() * (1 - t) + cb.green() * t),
        round(ca.blue() * (1 - t) + cb.blue() * t),
    ).name()


def _claro(cor: str) -> bool:
    c = QColor(cor)
    return (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000 > 128


def _luminancia(cor: str) -> float:
    c = QColor(cor)
    canais = []
    for v in (c.redF(), c.greenF(), c.blueF()):
        canais.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * canais[0] + 0.7152 * canais[1] + 0.0722 * canais[2]


def contraste(a: str, b: str) -> float:
    """Razão de contraste WCAG. 4.5 é o mínimo AA para texto normal."""
    maior, menor = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (maior + 0.05) / (menor + 0.05)


def _bolha(verde: str, fundo: str, texto: str, minimo: float = 4.5) -> str:
    """Bolha enviada com o verde do tema, escurecida (ou clareada) até o texto
    ficar legível em cima dela.

    Proporção fixa não serve: o mesmo 72% de mistura dá 6.8 no WhatsApp escuro e
    3.2 no Solarized. Acessibilidade não é lugar de chutar constante.
    """
    alvo = fundo if not _claro(fundo) else "#ffffff"
    melhor, melhor_c = _mix(verde, alvo, 0.6), 0.0
    for passo in range(0, 21):
        candidato = _mix(verde, alvo, passo / 20)
        c = contraste(texto, candidato)
        if c >= minimo:
            return candidato
        if c > melhor_c:
            melhor, melhor_c = candidato, c
    return melhor


def _superficie(base: str, texto: str, escape: str, minimo: float = 4.5) -> str:
    """Superfície que garante contraste com o texto, afastando-se de `escape`
    o mínimo necessário. O Solarized Light, por exemplo, é claro de propósito e
    a bolha recebida ficava em 4.0 sem isto."""
    if contraste(texto, base) >= minimo:
        return base
    for passo in range(1, 21):
        candidato = _mix(base, escape, passo / 20)
        if contraste(texto, candidato) >= minimo:
            return candidato
    return _mix(base, escape, 1.0)


def _badge_par(verde: str, bg: str, text: str) -> tuple[str, str]:
    """(preenchimento, cor do número) do badge de não lidas.

    Em vez de fixar o número em branco e depois torcer o verde, deixa o número
    escolher entre três candidatos — assim o verde do tema fica intacto na
    maioria dos casos e o par ainda passa em AA.
    """
    numero = max(("#ffffff", bg, text), key=lambda c: contraste(c, verde))
    if contraste(numero, verde) >= 4.5:
        return (verde, numero)
    escape = "#000000" if contraste(numero, "#ffffff") >= 2 else "#ffffff"
    return (_superficie(verde, numero, escape), numero)


def tema(bg, panel, panel2, line, accent, text, muted, dim, verde, vermelho) -> dict:
    """Monta a paleta do app a partir dos 10 tokens do painel-operador.

    ponytail: um gerador em vez de 14 paletas escritas à mão — tema novo é uma
    linha de tokens, e todos os estados saem coerentes de graça.
    """
    e_claro = _claro(bg)
    escape_sup = "#ffffff" if e_claro else "#000000"
    return dict(
        fundo=bg,
        painel=panel,
        # a bolha enviada puxa o verde do tema; a recebida é a superfície 2
        meu=_bolha(verde, bg, text),
        dele=_superficie(panel2, text, escape_sup),
        texto=text,
        fraco=_superficie(muted, bg, text, minimo=3.0),
        divisor=line,
        selecao=_mix(panel2, accent, 0.12),
        hover=_mix(panel, text, 0.06),
        barra=dim,
        hora_minha=_mix(verde, text, 0.45),
        accent=accent,
        perigo=vermelho,
        # o verde do badge e o azul da citação vêm do tema, não fixos
        badge=_badge_par(verde, bg, text)[0],
        sobre_badge=_badge_par(verde, bg, text)[1],
        # o mesmo verde nao serve de preenchimento E de texto: como texto ele e'
        # lido contra o painel, e ai precisa se afastar ate' passar em AA
        badge_texto=_superficie(verde, panel, text),
        citacao=accent,
        # dentro da bolha o secundario e' lido contra a BOLHA, nao contra o fundo
        fraco_meu=_superficie(_mix(verde, text, 0.45), _bolha(verde, bg, text), text),
        fraco_dele=_superficie(muted, _superficie(panel2, text, escape_sup), text),
        citacao_bolha=_superficie(accent, _bolha(verde, bg, text), text),
        aba_ativa=_bolha(accent, bg, text if e_claro else _mix(accent, text, 0.3)),
        texto_aba=text if e_claro else _mix(accent, text, 0.3),
        claro=e_claro,
    )


PALETAS = {
    # os dois primeiros são o WhatsApp de verdade; o resto vem do catálogo de
    # temas de editor do painel-operador (waba-dash)
    "whatsapp escuro": dict(
        fundo="#0b141a", painel="#111b21", meu="#005c4b", dele="#202c33",
        texto="#e9edef", fraco="#8696a0", divisor="#222d34", selecao="#2a3942",
        hover="#182229", barra="#374248", hora_minha="#9fc7ba",
        accent="#00a884", perigo="#f15c6d", badge="#25d366",
        sobre_badge="#0b141a", badge_texto="#25d366", citacao="#53bdeb",
        fraco_meu="#9fc7ba", fraco_dele="#8696a0", citacao_bolha="#8fd3f4",
        aba_ativa="#0a5c4a", texto_aba="#d1f4cc", claro=False,
    ),
    "whatsapp claro": dict(
        fundo="#efeae2", painel="#ffffff", meu="#d9fdd3", dele="#ffffff",
        texto="#111b21", fraco="#667781", divisor="#e9edef", selecao="#f0f2f5",
        hover="#f5f6f6", barra="#c4c9cc", hora_minha="#5b8c72",
        accent="#00a884", perigo="#e5453a", badge="#25d366",
        sobre_badge="#ffffff", badge_texto="#008069", citacao="#027eb5",
        fraco_meu="#4a7c63", fraco_dele="#667781", citacao_bolha="#026a96",
        aba_ativa="#d9fdd3", texto_aba="#014c3d", claro=True,
    ),
    "classic dark":     tema("#0a0a0c", "#16161a", "#1e1e24", "#2a2a32", "#e5c07b", "#e6e6ef", "#9898a8", "#5c5c6e", "#98c379", "#e06c75"),
    "catppuccin mocha": tema("#1e1e2e", "#262636", "#313244", "#45475a", "#f5c2e7", "#cdd6f4", "#a6adc8", "#6c7086", "#a6e3a1", "#f38ba8"),
    "gruvbox":          tema("#282828", "#32302f", "#3c3836", "#504945", "#d79921", "#ebdbb2", "#bdae93", "#7c6f64", "#b8bb26", "#fb4934"),
    "tokyo night":      tema("#1a1b26", "#24283b", "#292e42", "#3b4261", "#e0af68", "#c0caf5", "#9aa5ce", "#565f89", "#73daca", "#f7768e"),
    "dracula":          tema("#282a36", "#313340", "#3c3e50", "#4d5066", "#ffb86c", "#f8f8f2", "#bcbcc4", "#65657a", "#50fa7b", "#ff5555"),
    "nord":             tema("#2e3440", "#3b4252", "#434c5e", "#4c566a", "#ebcb8b", "#eceff4", "#b8c0d0", "#6f7d97", "#a3be8c", "#bf616a"),
    "rose pine":        tema("#191724", "#26233a", "#2a283e", "#403d52", "#f6c177", "#e0def4", "#908caa", "#6e6a86", "#31748f", "#eb6f92"),
    "monokai":          tema("#272822", "#33342f", "#3e3d32", "#49483e", "#e6db74", "#f8f8f0", "#a1a18f", "#64645e", "#a6e22e", "#f92672"),
    "everforest":       tema("#2d353b", "#333c43", "#3a454c", "#4a555c", "#dbbc7f", "#d3c6aa", "#9da9a0", "#6b7b78", "#a7c080", "#e67e80"),
    "classic light":    tema("#ffffff", "#f8f9fa", "#e9ecef", "#dee2e6", "#b8860b", "#1a1a2e", "#6c757d", "#adb5bd", "#2d8a4e", "#c92a2a"),
    "catppuccin latte": tema("#eff1f5", "#e6e9ef", "#dce0e8", "#ccd0da", "#df8e1d", "#4c4f69", "#8c8fa1", "#acb0be", "#40a02b", "#d20f39"),
    "rose pine dawn":   tema("#faf4ed", "#f2e9e1", "#e8ded2", "#dacbb8", "#b4637a", "#575279", "#9893a5", "#c4b9c8", "#56949f", "#b4637a"),
    "solarized light":  tema("#fdf6e3", "#eee8d5", "#e4ddc8", "#d3cbb3", "#b58900", "#586e75", "#839496", "#bdbb96", "#859900", "#dc322f"),
    "github light":     tema("#ffffff", "#f6f8fa", "#eaeef2", "#d0d7de", "#9a6700", "#1f2328", "#656d76", "#afb8c1", "#1a7f37", "#cf222e"),
}

EMOJI_TEMA = {
    "whatsapp escuro": "🟢", "whatsapp claro": "⚪", "classic dark": "⬛",
    "catppuccin mocha": "🐱", "gruvbox": "🟫", "tokyo night": "🌃",
    "dracula": "🧛", "nord": "❄️", "rose pine": "🌹", "monokai": "💛",
    "everforest": "🌿", "classic light": "⬜", "catppuccin latte": "�latte",
    "rose pine dawn": "🌅", "solarized light": "📜", "github light": "🐙",
}
EMOJI_TEMA["catppuccin latte"] = "☕"

# Pares (tinta, superfície) que precisam passar em AA. Toda paleta passa por
# aqui — inclusive as duas escritas à mão — para que tema novo nasça acessível.
# (tinta, superfície, mínimo). 4.5 = AA para texto; 1.25 no par bolha/fundo
# porque o WhatsApp oficial está em 1.30 — lá quem separa é a textura, não a cor.
PARES_AA = [
    ("fraco_meu", "meu"), ("fraco_dele", "dele"),
    ("citacao_bolha", "meu"), ("citacao", "dele"),
    ("sobre_badge", "badge"), ("badge_texto", "painel"),
    ("texto_aba", "aba_ativa"), ("hora_minha", "meu"),
    ("texto", "fundo"), ("texto", "meu"), ("texto", "dele"),
]
PARES_SUPERFICIE = [("dele", "fundo", 1.25)]


def garantir_aa(paleta: dict) -> dict:
    """Afasta cada tinta da sua superfície até cruzar 4.5.

    Repete até estabilizar: um par pode mexer numa cor que outro par já tinha
    usado como referência (ajustar `texto` desfaz o `badge_texto` derivado antes
    dele). Três passadas bastam — se não convergir, o tema é impossível.
    """
    for _ in range(3):
        antes = dict(paleta)
        for tinta, superficie in PARES_AA:
            if tinta not in paleta or superficie not in paleta:
                continue
            alvo = paleta["texto"] if tinta != "texto" else paleta["fundo"]
            cor = _superficie(paleta[tinta], paleta[superficie], alvo)
            if contraste(cor, paleta[superficie]) < 4.5:
                # o texto do tema não basta como destino: em temas de baixo
                # contraste por desenho (Solarized) o próprio texto empata em
                # 4.39. Segue até o extremo oposto da superfície.
                extremo = "#000000" if _claro(paleta[superficie]) else "#ffffff"
                cor = _superficie(paleta[tinta], paleta[superficie], extremo)
            paleta[tinta] = cor
        for tinta, superficie, minimo in PARES_SUPERFICIE:
            if contraste(paleta[tinta], paleta[superficie]) < minimo:
                extremo = "#000000" if _claro(paleta[superficie]) else "#ffffff"
                paleta[tinta] = _superficie(
                    paleta[tinta], paleta[superficie], extremo, minimo=minimo
                )
        if paleta == antes:
            break
    return paleta


PALETAS = {nome: garantir_aa(dict(pal)) for nome, pal in PALETAS.items()}

P = dict(PALETAS["whatsapp escuro"])  # paleta ativa; os delegates leem daqui


def tema_css() -> str:
    return f"""
QWidget {{ background:{P['painel']}; color:{P['texto']}; font-size:{ESCALA.px()}px; }}
QListView {{ background:{P['fundo']}; border:none; outline:none; }}
QListView#chats {{ background:{P['painel']}; border-right:1px solid {P['divisor']}; }}
QListView::item:selected {{ background:{P['selecao']}; }}
QTextEdit {{ background:{P['selecao']}; border:none; border-radius:8px; padding:8px; }}
QPushButton {{ background:transparent; border:none;
               font-size:{ESCALA.px() + 4}px; padding:{ESCALA.e(6)}px; }}
QPushButton:hover {{ background:{P['selecao']}; border-radius:6px; }}
QLabel#titulo {{ background:transparent; font-weight:600; font-size:{ESCALA.px()+1}px; }}
QStatusBar {{ background:{P['painel']}; color:{P['fraco']}; border-top:1px solid {P['divisor']}; }}
QSplitter::handle {{ background:{P['divisor']}; width:1px; }}
QScrollBar:vertical {{ background:transparent; width:8px; margin:0; }}
QScrollBar::handle:vertical {{ background:{P['barra']}; border-radius:4px; min-height:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
QDialog QLabel {{ background:transparent; }}
QLineEdit, QComboBox {{ background:{P['selecao']}; border:1px solid {P['divisor']};
                        border-radius:6px; padding:6px; }}
QDialogButtonBox QPushButton {{ background:{P['selecao']}; border-radius:6px;
                                font-size:14px; padding:6px 16px; }}
/* ponytail: `:checkable` NAO existe no Qt Style Sheets — a regra era morta e as
   abas caíam no font-size:18px do QPushButton geral, maiores que o nome do chat.
   Property seletor funciona. */
QPushButton[abaFiltro="true"] {{ background:{P['selecao']};
                         border-radius:{ESCALA.e(15)}px;
                         font-size:{ESCALA.px() - 2}px;
                         padding:{ESCALA.e(5)}px {ESCALA.e(14)}px; color:{P['fraco']}; }}
QPushButton[abaFiltro="true"]:checked {{ background:{P['aba_ativa']};
                         color:{P['texto_aba']}; }}
QPushButton[abaFiltro="true"]:hover {{ background:{_mix(P['selecao'], P['texto'], 0.10)}; }}
QPushButton[abaFiltro="true"]:checked:hover {{
                         background:{_mix(P['aba_ativa'], P['texto'], 0.10)}; }}
QPushButton:disabled {{ color:{P['barra']}; }}
QLineEdit:focus, QTextEdit:focus {{ border:1px solid {P['accent']}; }}
QMenu {{ background:{P['painel']}; border:1px solid {P['divisor']};
         padding:4px; border-radius:6px; }}
QMenu::item {{ padding:{ESCALA.e(6)}px {ESCALA.e(18)}px; border-radius:4px; }}
QMenu::item:selected {{ background:{P['selecao']}; color:{P['texto']}; }}
QMenu::separator {{ height:1px; background:{P['divisor']}; margin:4px 8px; }}
QComboBox QAbstractItemView {{ background:{P['painel']}; color:{P['texto']};
         border:1px solid {P['divisor']};
         selection-background-color:{P['selecao']};
         selection-color:{P['texto']}; }}
QToolTip {{ background:{P['painel']}; color:{P['texto']};
            border:1px solid {P['divisor']}; padding:4px; }}
/* ---- header da conversa (spec 2.0: sem linha, zonas clicáveis pontuais) ---- */
QWidget#zona_nome {{ background:transparent; border:none; }}
QPushButton#btn_busca, QPushButton#btn_menu {{
    border:none; border-radius:{ESCALA.e(18)}px; background:transparent;
    font-size:{ESCALA.px() + 4}px;
    min-width:36px; max-width:36px; min-height:36px; max-height:36px;
    padding:0; color:{P['fraco']}; }}
QPushButton#btn_busca:hover, QPushButton#btn_menu:hover {{ background:{P['hover']}; }}
QPushButton#btn_busca:pressed, QPushButton#btn_menu:pressed {{ background:{P['selecao']}; }}
QPushButton#btn_busca[active="true"] {{ background:{P['selecao']}; color:{P['accent']}; }}
/* ---- faixa do usuário (topo da sidebar) ---- */
QWidget#faixa_usuario {{ background:{P['painel']}; border:none; }}
QPushButton#avatar_usuario {{
    border:none; border-radius:20px; background:transparent;
    min-width:40px; max-width:40px; min-height:40px; max-height:40px;
    padding:0; }}
QPushButton#avatar_usuario:hover {{ background:{P['hover']}; }}
QLabel#nome_usuario {{ background:transparent; font-size:{ESCALA.px() + 1}px;
                       font-weight:500; color:{P['texto']}; }}
"""


CORES_AVATAR = ["#e17076", "#7bc862", "#65aadd", "#a695e7", "#ee7aae", "#faa774", "#6ec9cb"]


def fonte(base: QFont, delta: float = 0, negrito: bool = False) -> QFont:
    """ponytail: o stylesheet define font-size em px, entao pointSizeF() volta -1
    e somar nele gera 'Point size <= 0'. Aqui um caminho para cada caso."""
    f = QFont(base)
    if base.pointSizeF() > 0:
        f.setPointSizeF(max(6.0, base.pointSizeF() + delta))
    else:
        f.setPixelSize(max(8, int(base.pixelSize() + delta * 1.4)))
    f.setBold(negrito)
    return f


@lru_cache(maxsize=256)
def cor_autor(jid: str) -> str:
    """Cor estável por participante, como no WhatsApp.

    Gira o matiz do accent em passos de 30° e procura brilho/saturação que
    passem em AA contra a bolha. Misturar com o texto colapsaria os matizes —
    em tema claro sobrariam 4 cores distintas em vez de 12.
    """
    base = QColor(P["accent"])
    h = ((base.hue() if base.hue() >= 0 else 140) + (sum(map(ord, jid)) % 12) * 30) % 360
    for s in range(max(140, base.saturation()), 39, -25):
        for v in (range(235, 55, -9) if P["claro"] else range(150, 256, 6)):
            c = QColor.fromHsv(h, s, v).name()
            if contraste(c, P["dele"]) >= 4.5:
                return c
    return P["texto"]


@lru_cache(maxsize=200)
def foto(caminho: str, lado: int = 44) -> QPixmap:
    """Foto de perfil recortada em círculo. Vazia se o arquivo não presta."""
    origem = QPixmap(caminho)
    if origem.isNull():
        return QPixmap()
    origem = origem.scaled(lado, lado, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    pix = QPixmap(lado, lado)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    caminho_circular = QPainterPath()
    caminho_circular.addEllipse(0, 0, lado, lado)
    p.setClipPath(caminho_circular)
    p.drawPixmap((lado - origem.width()) // 2, (lado - origem.height()) // 2, origem)
    p.end()
    return pix


@lru_cache(maxsize=400)
def avatar(nome: str, jid: str, lado: int = 44) -> QPixmap:
    """Iniciais em círculo colorido — o fallback de quem não tem foto."""
    pix = QPixmap(lado, lado)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(CORES_AVATAR[sum(map(ord, jid)) % len(CORES_AVATAR)]))
    p.drawEllipse(0, 0, lado, lado)
    partes = [x for x in nome.split() if x[:1].isalnum()][:2]
    iniciais = "".join(x[0].upper() for x in partes) or "#"
    p.setPen(QColor("#ffffff"))
    f = p.font()
    f.setPointSize(int(lado * 0.32))
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRect(0, 0, lado, lado), Qt.AlignCenter, iniciais)
    p.end()
    return pix


def _cru(dado) -> bytes:
    """Bytes da miniatura embutida, venha ela como vier.

    A REST devolve `jpegThumbnail` em base64; o socket.io entrega o MESMO campo
    em bytes crus (o payload dele é binário). Sem esta ponte, o b64decode
    levantava em toda mídia que chegava ao vivo, thumb_embutida devolvia um
    QPixmap nulo — e a bolha ficava vazia até recarregar a conversa.
    """
    if isinstance(dado, (bytes, bytearray)):
        return bytes(dado)
    return base64.b64decode(dado)


@lru_cache(maxsize=200)
def onda_do_payload(bruto) -> tuple[float, ...]:
    """Onda que o próprio WhatsApp mandou: 64 amostras de 0 a 100 → 0.0 a 1.0.

    É a mesma que o Web desenha. Chega em base64 pela REST e em bytes crus pelo
    socket, como toda a mídia embutida — daí o `_cru`.
    """
    try:
        b = _cru(bruto)
    except Exception:
        return ()
    return tuple(min(1.0, v / 100) for v in b)


@lru_cache(maxsize=200)
def thumb_embutida(b64, lado: int = 260) -> QPixmap:
    """Miniatura que veio dentro da própria mensagem — sem ir à rede."""
    pix = QPixmap()
    try:
        pix.loadFromData(_cru(b64))
    except Exception:
        return QPixmap()
    if pix.isNull():
        return QPixmap()
    return pix.scaled(lado, lado, Qt.KeepAspectRatio, Qt.SmoothTransformation)


@lru_cache(maxsize=60)
def miniatura(caminho: str) -> QPixmap:
    """ponytail: lru_cache no lugar de um gerenciador de cache. Miniatura sai do
    disco, nunca do heap da mensagem — e a lru descarta sozinha."""
    return QPixmap(caminho).scaled(280, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation)


@lru_cache(maxsize=120)
def link_pix(b64, largura: int) -> QPixmap:
    """Thumb do preview de link pronta para o cartão.

    O WhatsApp Web mostra a imagem wide (proporção ~5:2) no topo do cartão.
    A thumb embutida no payload é pequena (~140px) e pode ter outra proporção:
    escala para COBRIR a área e corta o centro — mesmo comportamento do Web.
    """
    pix = QPixmap()
    try:
        pix.loadFromData(_cru(b64))
    except Exception:
        return QPixmap()
    if pix.isNull():
        return QPixmap()
    altura = max(1, round(largura * 0.4))
    if pix.width() != largura or pix.height() != altura:
        escala = max(largura / pix.width(), altura / pix.height())
        if abs(escala - 1.0) > 0.01:
            pix = pix.scaled(
                max(1, round(pix.width() * escala)),
                max(1, round(pix.height() * escala)),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        x = max(0, (pix.width() - largura) // 2)
        y = max(0, (pix.height() - altura) // 2)
        pix = pix.copy(x, y, min(largura, pix.width()), min(altura, pix.height()))
    return pix


class Animador(QObject):
    """GIF que anima DENTRO da bolha, como no WhatsApp Web.

    O Web usa `<video autoplay loop muted>`: o GIF do WhatsApp é um MP4 sem
    áudio com `gifPlayback:true`. Aqui é a mesma coisa — QMediaPlayer em loop
    infinito, sem saída de áudio e sem widget de vídeo: o QVideoSink entrega o
    quadro e quem pinta continua sendo o delegate, então a bolha não vira um
    widget dentro da lista.

    Só toca o que está na tela. Cada `paint` carimba o mid; quem passa de
    OCIOSO_MS sem ser pintado (rolou para fora) é desligado, e no máximo TETO
    players existem ao mesmo tempo — um GIF por bolha visível é o teto real.
    """

    quadro = Signal(str)  # mid — a Janela repinta SÓ aquela linha

    TETO = 4
    OCIOSO_MS = 1500
    INTERVALO_MS = 66  # ~15 quadros/s ja' e' fluido; o resto so' gasta CPU

    def __init__(self):
        super().__init__()
        self.vivos: dict[str, dict] = {}
        self._faxina = QTimer(self)
        self._faxina.timeout.connect(self._podar)
        self._faxina.start(1000)

    def quadro_de(self, mid: str, caminho: str) -> QPixmap | None:
        """Quadro atual do GIF — sobe o player na primeira vez que é pedido.

        Chamado de dentro do paint: é lá que se sabe quais bolhas estão na tela.
        """
        if not mid or not caminho:
            return None
        v = self.vivos.get(mid)
        if v is None:
            if len(self.vivos) >= self.TETO:
                self._parar(min(self.vivos, key=lambda k: self.vivos[k]["visto"]))
            v = self._subir(mid, caminho)
        v["visto"] = datetime.now().timestamp()
        return v["pix"]

    def _subir(self, mid: str, caminho: str) -> dict:
        pl = QMediaPlayer(self)
        # o sink e' filho do player: morre junto, sem ponteiro solto no meio
        sink = QVideoSink(pl)
        pl.setVideoSink(sink)
        pl.setLoops(QMediaPlayer.Infinite)
        v = {"pl": pl, "pix": None, "visto": 0.0, "ultimo": 0.0}
        self.vivos[mid] = v

        def chegou(f):
            agora = datetime.now().timestamp()
            if not f.isValid() or (agora - v["ultimo"]) * 1000 < self.INTERVALO_MS:
                return
            img = f.toImage()
            if img.isNull():
                return
            v["ultimo"] = agora
            v["pix"] = QPixmap.fromImage(img)
            self.quadro.emit(mid)

        sink.videoFrameChanged.connect(chegou)
        pl.setSource(QUrl.fromLocalFile(caminho))
        pl.play()
        return v

    def _parar(self, mid: str):
        if v := self.vivos.pop(mid, None):
            v["pl"].stop()
            v["pl"].deleteLater()

    def _podar(self):
        limite = datetime.now().timestamp() - self.OCIOSO_MS / 1000
        for mid in [k for k, v in self.vivos.items() if v["visto"] < limite]:
            self._parar(mid)


ANIMA: Animador | None = None  # criado no main(), depois do QApplication


def dominio_de(url: str) -> str:
    """'https://www.exemplo.com.br/x' -> 'exemplo.com.br' — o rótulo verde
    do cartão de link no WhatsApp Web."""
    try:
        host = (urlparse(url).netloc or url).lower()
    except Exception:
        host = url.lower()
    return host.removeprefix("www.") or host


class ItemChat(QStyledItemDelegate):
    """Linha da barra lateral no formato do WhatsApp Web: avatar, nome, marca de
    leitura + autor + prévia, hora, contador de não lidas, sino e pino."""

    ALTURA, PAD, LADO = 72, 12, 49

    def sizeHint(self, opt, ix):
        return QSize(0, self.ALTURA)

    def paint(self, p, opt, ix):
        chat = ix.data(Qt.UserRole) or {}
        nome = ix.data(Qt.DisplayRole) or ""
        jid = chat.get("remoteJid", "")
        naolidas = int(chat.get("unreadCount") or 0)
        mudo = bool(chat.get("_mudo"))
        fixado = bool(chat.get("_fixado"))
        marca, autor, corpo = api.previa(chat)
        azul_marca = api.marca_de(chat.get("lastMessage") or {})[1]
        ts = (chat.get("lastMessage") or {}).get("messageTimestamp")

        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if opt.state & QStyle.State_Selected:
            p.fillRect(opt.rect, QColor(P["selecao"]))
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(opt.rect, QColor(P["hover"]))

        r = opt.rect
        retrato = foto(chat["_foto"], self.LADO) if chat.get("_foto") else QPixmap()
        if retrato.isNull():
            retrato = avatar(nome, jid, self.LADO)
        p.drawPixmap(r.left() + self.PAD, r.top() + self.PAD, retrato)

        x = r.left() + self.PAD + self.LADO + self.PAD
        direita = 64 if (naolidas or mudo or fixado) else 46
        largura = max(60, r.width() - (x - r.left()) - direita)

        # linha 1: nome + hora
        f_nome = fonte(opt.font, 0.5)
        p.setFont(f_nome)
        p.setPen(QColor(P["texto"]))
        p.drawText(
            QRect(x, r.top() + 12, largura, 20), Qt.AlignLeft | Qt.AlignVCenter,
            QFontMetrics(f_nome).elidedText(nome, Qt.ElideRight, largura),
        )
        if ts and r.width() > 210:
            p.setFont(fonte(opt.font, -2.5))
            p.setPen(QColor(P["badge_texto"] if naolidas else P["fraco"]))
            p.drawText(QRect(r.right() - 58, r.top() + 13, 50, 16), Qt.AlignRight, hora(ts))

        if andamento := ix.data(Qt.UserRole + 1):
            # enquanto carrega, a prévia dá lugar à barra — assim dá para
            # acompanhar de fora da conversa; volta a ser prévia ao terminar
            feito, alvo, etapa = andamento
            fracao = min(1.0, feito / max(1, alvo))
            trilho = QRect(x, r.top() + ESCALA.e(40), largura - ESCALA.e(34), ESCALA.e(4))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(P["divisor"]))
            p.drawRoundedRect(trilho, 2, 2)
            p.setBrush(QColor(P["accent"]))
            p.drawRoundedRect(
                QRect(trilho.left(), trilho.top(), int(trilho.width() * fracao),
                      trilho.height()), 2, 2)
            f_pct = fonte(opt.font, -2.5)
            p.setFont(f_pct)
            p.setPen(QColor(P["fraco"]))
            p.drawText(QRect(trilho.right() + ESCALA.e(6), r.top() + ESCALA.e(34),
                             ESCALA.e(30), ESCALA.e(16)),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{int(fracao * 100)}%")
            p.setPen(QColor(P["divisor"]))
            p.drawLine(x, r.bottom(), r.right(), r.bottom())
            p.restore()
            return

        # linha 2: ✓✓ Autor: prévia
        f_previa = fonte(opt.font, -1)
        p.setFont(f_previa)
        metrica = QFontMetrics(f_previa)
        cursor = x
        limite = x + largura
        if marca:
            p.setPen(QColor(P["citacao"] if azul_marca else P["fraco"]))
            p.drawText(QRect(cursor, r.top() + 36, 26, 18), Qt.AlignLeft | Qt.AlignVCenter, marca)
            cursor += metrica.horizontalAdvance(marca) + 5
        if autor:
            rotulo = f"{autor}: "
            p.setPen(QColor(P["fraco"]))
            p.drawText(QRect(cursor, r.top() + 36, limite - cursor, 18),
                       Qt.AlignLeft | Qt.AlignVCenter, rotulo)
            cursor += metrica.horizontalAdvance(rotulo)
        if corpo and cursor < limite:
            p.setPen(QColor(P["fraco"]))
            p.drawText(
                QRect(cursor, r.top() + 36, limite - cursor, 18), Qt.AlignLeft | Qt.AlignVCenter,
                metrica.elidedText(corpo, Qt.ElideRight, limite - cursor),
            )

        # coluna da direita: sino cortado, contador, pino
        dx = r.right() - 12
        if naolidas:
            rotulo = "999+" if naolidas > 999 else str(naolidas)
            f_badge = fonte(opt.font, -2.5, negrito=True)
            p.setFont(f_badge)
            w = max(20, QFontMetrics(f_badge).horizontalAdvance(rotulo) + 12)
            pill = QRect(dx - w, r.top() + 38, w, 20)
            # o verde puro sobre painel claro fica em 1.98 (o WhatsApp Web real
            # também) — um anel resolve o limite gráfico sem mexer na cor
            p.setPen(QPen(QColor(_mix(P["badge"], P["texto"], 0.35)), 1))
            p.setBrush(QColor(P["badge"]))
            p.drawRoundedRect(pill, 10, 10)
            p.setPen(QColor(P["sobre_badge"]))
            p.drawText(pill, Qt.AlignCenter, rotulo)
            dx -= w + 6
        if mudo:
            p.setFont(fonte(opt.font, -1))
            p.setPen(QColor(P["fraco"]))
            p.drawText(QRect(dx - 20, r.top() + 38, 20, 20), Qt.AlignCenter, "🔕")
            dx -= 24
        if fixado:
            p.setFont(fonte(opt.font, -1))
            p.setPen(QColor(P["fraco"]))
            p.drawText(QRect(dx - 18, r.top() + 38, 18, 20), Qt.AlignCenter, "📌")

        p.setPen(QColor(P["divisor"]))
        p.drawLine(x, r.bottom(), r.right(), r.bottom())
        p.restore()


@lru_cache(maxsize=300)
def documento(texto: str, largura: int, px: int, cor: str, italico: bool) -> QTextDocument:
    """Texto do balão como documento rico. `drawText` não interpreta marcação —
    era por isso que os asteriscos do WhatsApp apareciam crus na tela."""
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    f = QFont()
    f.setPixelSize(px)
    f.setItalic(italico)
    doc.setDefaultFont(f)
    opcao = QTextOption()
    opcao.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(opcao)
    doc.setHtml(f"<span style='color:{cor}'>{api.formatar(texto)}</span>")
    doc.setTextWidth(largura)
    return doc


def id_citado(msg: dict) -> str:
    """ID da mensagem citada (`stanzaId`) — o alvo do pulo ao clicar na citação."""
    ctx = msg.get("contextInfo") or (msg.get("message") or {}).get(
        "extendedTextMessage", {}
    ).get("contextInfo") or {}
    return (ctx.get("stanzaId") or "") if ctx.get("quotedMessage") else ""


def citacao_de(msg: dict) -> tuple[str, str, str] | None:
    """(autor, trecho, miniatura base64) da mensagem citada, ou None."""
    ctx = msg.get("contextInfo") or (msg.get("message") or {}).get(
        "extendedTextMessage", {}
    ).get("contextInfo") or {}
    citada = ctx.get("quotedMessage")
    if not citada:
        return None

    # a primeira chave pode ser metadado (messageContextInfo); vale a que tem
    # conteúdo de verdade, senão o trecho citado sai em branco
    tipo = next(
        (k for k in citada if k not in ("messageContextInfo", "senderKeyDistributionMessage")),
        next(iter(citada), ""),
    )
    if tipo == "audioMessage":
        # O Web mostra o MICROFONE e a DURAÇÃO do áudio citado, não a palavra
        # "Áudio" — `quotedMessage.audioMessage.seconds` vem no payload.
        a_ = citada.get("audioMessage") or {}
        seg = api._num(a_.get("seconds"))
        corpo = f"{'🎤' if a_.get('ptt') else '🎵'} {seg // 60}:{seg % 60:02d}"
    else:
        corpo = api.text_of({"message": citada, "messageType": tipo}).strip()
    if not corpo:
        corpo = api.ICONES.get(tipo, "") + " mensagem" if tipo else "mensagem"

    bruto = ctx.get("participant") or ""
    if ctx.get("stanzaId") in MINHAS or bruto in EU_JIDS or (
        (msg.get("key") or {}).get("fromMe") and not bruto
    ):
        autor = "Você"
    else:
        autor = (
            (ctx.get("quotedPushName") or "").strip()
            or (CONTATOS.get(bruto) or "").strip()
            or api.formatar_numero(bruto)
        )

    sub = citada.get(tipo)
    thumb = sub.get("jpegThumbnail", "") if isinstance(sub, dict) else ""
    return (autor, corpo[:90] or "mídia", thumb)


CORES_DOC = {
    "pdf": "#e5453a", "doc": "#3b7bd8", "docx": "#3b7bd8", "xls": "#1d8b48",
    "xlsx": "#1d8b48", "csv": "#1d8b48", "ppt": "#e07a34", "pptx": "#e07a34",
    "zip": "#8a6fd4", "rar": "#8a6fd4", "7z": "#8a6fd4",
}


def cor_doc(nome: str) -> str:
    return CORES_DOC.get(nome.rsplit(".", 1)[-1].lower() if "." in nome else "", "#6b7a82")


class Balao(QStyledItemDelegate):
    """Bolha de conversa. Cada tipo tem seu cartão, como no WhatsApp Web:
    áudio com onda e duração, documento com ícone e tamanho, localização com
    pino, contato com nome, e link com título."""

    MAX = 0.66

    def __init__(self):
        super().__init__()
        # Retratos do cartão de voz (o Web mostra a foto de quem falou).
        # A Janela carimba: `contato` ao abrir a conversa, `eu` uma vez no boot.
        self.contato: tuple[str, str, str] = ("", "", "")  # (nome, jid, caminho)
        self.eu: tuple[str, str, str] = ("", "", "")

    # ponytail: property em vez de constante — sem isto nada acompanha o Ctrl+=.
    # Um round() por acesso custa ~80ns; o QFontMetrics do mesmo _medir custa
    # ordem de grandeza mais. Cachear seria otimizar o item mais barato.
    @property
    def MARGEM(self): return ESCALA.e(10)
    @property
    def PAD_H(self): return ESCALA.e(9)
    @property
    def PAD_V(self): return ESCALA.e(6)
    @property
    def PAD(self): return ESCALA.e(9)
    @property
    def DIA(self): return ESCALA.e(30)
    @property
    def REACAO(self): return ESCALA.e(22)
    @property
    def CITACAO(self): return ESCALA.e(46)
    @property
    def CARTAO(self): return ESCALA.e(56)
    @property
    def LINK(self): return ESCALA.e(66)
    @property
    def AUTOR(self): return ESCALA.e(20)
    @property
    def AVATAR(self): return ESCALA.e(38)
    @property
    def RECUO(self): return ESCALA.e(48)

    @staticmethod
    def _nome_autor(msg: dict) -> str:
        """Nome do autor em grupo.

        O `pushName` às vezes vem sendo o próprio número — aí não é nome, e o
        LID cru na tela não diz nada a ninguém.
        """
        part = (msg.get("key") or {}).get("participant", "")
        empurrado = (msg.get("pushName") or "").strip()
        if empurrado and not empurrado.replace("+", "").replace(" ", "").isdigit():
            return empurrado
        if nome := (CONTATOS.get(part) or "").strip():
            return nome
        alt = (msg.get("key") or {}).get("participantAlt") or ""
        numero = api.formatar_numero(alt or part)
        return numero if numero != "Contato" else "Participante"

    def _cabeca(self, ix, msg: dict) -> bool:
        """Só em grupo, só em recebida, e só na primeira de uma sequência."""
        k = msg.get("key") or {}
        return (
            str(k.get("remoteJid", "")).endswith("@g.us")
            and not k.get("fromMe")
            and not self._mesmo_autor(ix)
        )

    def _em_grupo(self, msg: dict) -> bool:
        k = msg.get("key") or {}
        return str(k.get("remoteJid", "")).endswith("@g.us") and not k.get("fromMe")

    @staticmethod
    def _rodape_de(msg) -> tuple[str, str, bool]:
        """(rótulo, marca, azul) — medido em _medir e pintado em paint."""
        marca, azul = api.marca_de(msg)
        rotulo = ("★ " if msg.get("_favorita") else "") + (
            "Editada " if api.foi_editada(msg) else ""
        ) + hora(msg.get("messageTimestamp", 0))
        return rotulo, marca, azul

    @staticmethod
    def _autor_de(msg: dict) -> str:
        """Quem falou: em grupo vem de participant, em 1:1 basta fromMe."""
        k = msg.get("key") or {}
        return "eu" if k.get("fromMe") else (
            k.get("participant") or k.get("remoteJid") or "ele"
        )

    def _mesmo_autor(self, ix) -> bool:
        """Anterior do mesmo autor, no mesmo dia, a menos de 5 min."""
        if ix.row() == 0 or self._vira_o_dia(ix):
            return False
        atual = ix.data(Qt.UserRole) or {}
        antes = ix.model().index(ix.row() - 1).data(Qt.UserRole) or {}
        if self._autor_de(atual) != self._autor_de(antes):
            return False
        return 0 <= atual.get("messageTimestamp", 0) - antes.get("messageTimestamp", 0) <= 300

    def _dados(self, ix, total):
        msg = ix.data(Qt.UserRole) or {}
        texto = api.text_of(msg)
        if api.foi_apagada(msg):
            texto = "🚫  " + texto  # medido junto, senao a bolha corta o rotulo
        det = {} if api.foi_apagada(msg) else api.detalhes(msg)
        # HD: recebida vem marcada no payload (midQualityFileSha256); a nossa
        # enviada em HD está no mapa local — as duas viram o mesmo selo
        if det.get("tipo") == "imageMessage":
            det["hd"] = bool(det.get("hd")) or (
                (msg.get("key") or {}).get("id") in HD_IDS
            )
        pix = None
        caminho = msg.get("_arquivo")
        if not api.foi_apagada(msg):
            if caminho and pathlib.Path(caminho).suffix.lower() in (
                ".jpg", ".jpeg", ".png", ".webp", ".gif"
            ):
                pix = miniatura(caminho)
            elif det.get("thumb") and det.get("tipo") in (
                "imageMessage", "videoMessage", "documentMessage"
            ):
                # a miniatura do payload aparece na hora; o arquivo cheio
                # substitui quando terminar de baixar
                pix = thumb_embutida(det["thumb"], ESCALA.e(260))
        return msg, texto, pix, det, int(total * self.MAX)

    def _cartao(self, det: dict) -> str | None:
        """Qual cartão especial esta mensagem usa, se algum."""
        tipo = det.get("tipo")
        if tipo == "audioMessage":
            return "audio"
        if tipo == "documentMessage":
            return "documento"
        if tipo == "locationMessage":
            return "local"
        if tipo in ("contactMessage", "contactsArrayMessage") and det.get("contato"):
            return "contato"
        if det.get("link"):
            return "link"
        return None

    def _vira_o_dia(self, ix) -> str:
        modelo = ix.model()
        atual = datetime.fromtimestamp((ix.data(Qt.UserRole) or {}).get("messageTimestamp", 0))
        if ix.row() == 0:
            return atual.strftime("%d/%m/%Y")
        antes = datetime.fromtimestamp(
            (modelo.index(ix.row() - 1).data(Qt.UserRole) or {}).get("messageTimestamp", 0)
        )
        return atual.strftime("%d/%m/%Y") if atual.date() != antes.date() else ""

    def _medir(self, opt, ix):
        msg, texto, pix, det, teto = self._dados(ix, opt.rect.width() or 700)
        if self._em_grupo(msg):
            teto -= self.RECUO  # a bolha não pode comer o avatar do autor
        util = teto - self.PAD_H * 2
        cartao = self._cartao(det)
        alt_txt = larg_txt = alt_linha = 0
        mostra_texto = bool(texto) and cartao not in ("audio", "documento", "local", "contato")
        if pix is not None and texto.strip() in ("🎬 Vídeo", "🖼 Imagem"):
            mostra_texto = False
        if mostra_texto:
            # com mídia, a legenda quebra na largura DELA — senão uma legenda de
            # uma linha estica a bolha e a miniatura estreita deixa um vazio
            larg_doc = max(pix.width(), ESCALA.e(220)) if pix else util
            doc = documento(texto, min(larg_doc, util), ESCALA.px(),
                            P["texto"], api.foi_apagada(msg))
            alt_txt = int(doc.size().height())
            larg_txt = min(int(doc.idealWidth()) + 2, util)
            alt_linha = QFontMetrics(doc.defaultFont()).lineSpacing()

        # o rodapé é medido, não estimado: é ele que decide se cabe na última linha
        f_rod = fonte(opt.font, -2.5)
        m_rod = QFontMetrics(f_rod)
        rotulo, marca, _azul = self._rodape_de(msg)
        larg_rodape = m_rod.horizontalAdvance(rotulo) + ESCALA.e(8) + (
            m_rod.horizontalAdvance(marca) + ESCALA.e(4) if marca else 0
        )
        alt_rodape = m_rod.height()

        larg = max(larg_txt, min(pix.width(), util) if pix else 0)
        if self._cabeca(ix, msg):
            # o nome pode ser mais largo que o conteúdo
            larg = max(larg, min(util, QFontMetrics(fonte(opt.font, -0.5, negrito=True))
                                 .horizontalAdvance(self._nome_autor(msg)) + ESCALA.e(24)))
        # o WhatsApp põe a hora na mesma linha quando cabe — sem isto toda bolha
        # de uma linha fica ~50% mais alta do que precisa
        inline = (
            mostra_texto and not cartao and not pix
            and alt_txt <= alt_linha + 2 and larg_txt + larg_rodape <= util
        )
        if inline:
            larg = larg_txt + larg_rodape
            alto = self.PAD_V * 2 + alt_txt
        else:
            alto = self.PAD_V * 2 + alt_txt + alt_rodape

        if citado := citacao_de(msg):
            f_cit = fonte(opt.font, -2)
            m_cit = QFontMetrics(f_cit)
            preciso = max(
                m_cit.horizontalAdvance(citado[0]),
                m_cit.horizontalAdvance(citado[1][:60]),
            ) + ESCALA.e(16) + (ESCALA.e(46) if citado[2] else 0)
            larg = max(larg, min(util, preciso))
            alto += self.CITACAO
        if pix:
            alto += pix.height() + (ESCALA.e(6) if mostra_texto else 0)
        if cartao == "audio":
            larg = max(larg, min(ESCALA.e(310), util))
            alto += self.CARTAO - alt_rodape  # a hora vai para dentro do cartão
        elif cartao in ("documento", "contato"):
            larg = max(larg, min(ESCALA.e(270), util))
            alto += self.CARTAO
        elif cartao == "local":
            larg = max(larg, min(ESCALA.e(260), util))
            alto += ESCALA.e(128)
        elif cartao == "link":
            link = det.get("link") or {}
            com_thumb = bool(link.get("thumb"))
            l_cartao = min(ESCALA.e(280) if com_thumb else ESCALA.e(260), util)
            larg = max(larg, l_cartao)
            # a faixa do cartão ocupa a largura interna da bolha (o retorno do
            # _medir soma PAD_H*2 à largura; PAD == PAD_H, então a faixa = larg).
            # _medir e paint compartilham det: a imagem nunca dessincroniza.
            link["_img_larg"] = larg
            link["_img_h"] = round(larg * 0.4) if com_thumb else 0
            alto += link["_img_h"] + self.LINK
        if self._cabeca(ix, msg):
            alto += self.AUTOR
        if msg.get("_reacoes"):
            alto += self.REACAO
        minimo = larg_rodape + ESCALA.e(24)
        return (
            msg, texto, pix, det,
            min(max(larg + self.PAD_H * 2, minimo), teto),
            alto, mostra_texto,
        )

    def sizeHint(self, opt, ix):
        *_, _larg, alto, _ = self._medir(opt, ix)
        # QSize(0, …) faz o item ocupar a largura do viewport. Devolver a largura
        # da BOLHA fazia o QListView reservar conteúdo mais largo que a janela.
        respiro = ESCALA.e(2) if self._mesmo_autor(ix) else self.MARGEM
        return QSize(0, alto + respiro + (self.DIA if self._vira_o_dia(ix) else 0))

    # ---------- cartões ----------

    def _area_seta(self, caixa: QRect, meu: bool) -> QRect:
        """➦ de encaminhar no hover, FORA da bolha, no lado do espaço livre —
        como o Web: recebidas ganham a seta à direita; enviadas, à esquerda."""
        tam = ESCALA.e(28)
        if meu:
            return QRect(caixa.left() - tam - ESCALA.e(4), caixa.top(), tam, tam)
        return QRect(caixa.right() + ESCALA.e(4), caixa.top(), tam, tam)

    def _area_midia(self, caixa: QRect, y: int, pix: QPixmap) -> QRect:
        """Onde a imagem/vídeo REALMENTE está — a mesma conta do paint.

        No Web só a mídia abre o visualizador. O item do QListView ocupa a
        largura inteira do viewport, então sem esta área o clique no vazio ao
        lado da bolha abria a imagem.
        """
        return QRect(caixa.left() + self.PAD, y, pix.width(), pix.height())

    def _area_citacao(self, caixa: QRect, y: int) -> QRect:
        """Faixa da citação. `y` vem do geometria_do_item, que JÁ somou a
        citação — por isso volta CITACAO atrás."""
        return QRect(caixa.left() + self.PAD, y - self.CITACAO,
                     caixa.width() - self.PAD * 2, self.CITACAO - 6)

    def _area_local(self, caixa: QRect, y: int) -> QRect:
        """Área clicável do cartão de localização (mesma geometria do paint)."""
        return QRect(caixa.left() + self.PAD, y + 2,
                     caixa.width() - self.PAD * 2, 122)

    def geometria_audio(self, caixa: QRect, y: int):
        """(botão, onda) dentro da bolha — desenho e clique usam a MESMA caixa.

        O conteúdo do cartão flui da esquerda da bolha (caixa.left()+PAD), como
        os demais cartões. Antes a geometria era calculada contra o viewport com
        a MARGEM fixa de 10px, mas a bolha é posicionada com margem proporcional
        (5%) — em janela larga o áudio nascia ~30px FORA da bolha, e em grupo
        recuado vazava à esquerda.
        """
        lado = ESCALA.e(34)
        retrato = QRect(caixa.right() - self.PAD - lado, y + 2, lado, lado)
        botao = QRect(caixa.left() + self.PAD, y + 4, 34, 34)
        onda = QRect(botao.right() + 10, y + 4,
                     max(20, retrato.left() - 10 - botao.right() - 10), 34)
        return botao, onda, retrato

    def geometria_do_item(self, opt, ix, msg: dict, larg: int, alto: int):
        """(caixa da bolha, y do conteúdo) — replicação EXATA do paint.

        O hit-test de áudio precisa saber onde o desenho realmente está:
        margem proporcional, recuo de grupo, separador de dia, respiro entre
        bolhas do mesmo autor, cabeçalho de autor e citação — tudo na mesma
        ordem do paint.
        """
        topo = opt.rect.top()
        if self._vira_o_dia(ix):
            topo += self.DIA
        margem = max(ESCALA.e(8), int(opt.rect.width() * 0.05))
        recuo = self.RECUO if self._em_grupo(msg) else 0
        meu = bool((msg.get("key") or {}).get("fromMe"))
        x = (
            opt.rect.right() - larg - margem
            if meu else opt.rect.left() + margem + recuo
        )
        respiro = ESCALA.e(2) if self._mesmo_autor(ix) else self.MARGEM
        caixa = QRect(x, topo + respiro // 2, larg, alto)
        y = caixa.top() + self.PAD_V
        if self._cabeca(ix, msg):
            y += self.AUTOR
        if citacao_de(msg):
            y += self.CITACAO
        return caixa, y

    def _onda(self, p, r: QRect, cor: str, semente: int, fracao: float = 0.0,
              picos: list[float] | None = None):
        """Perfil da onda. Com picos decodificados (api.picos_de_audio) as
        barras seguem o áudio de verdade; sem arquivo ainda, caem no hash do
        id — a parte já tocada é pintada em destaque (sensação de progresso)."""
        p.setPen(Qt.NoPen)
        n = max(12, r.width() // 5)
        corte = int(n * fracao)
        # ponytail: nada de hex com alpha — o Qt le 9 digitos como #AARRGGBB,
        # entao "#ffffff30" virava amarelo OPACO em todos os temas.
        apagada = QColor(P["fraco"])
        apagada.setAlpha(110)
        altura_max = max(6, r.height() - 8)
        for i in range(n):
            if picos:
                idx = min(len(picos) - 1, i * len(picos) // max(1, n))
                h = 3 + int(round(picos[idx] * (altura_max - 3)))
            else:
                h = 3 + ((semente + i * 37) % 13)
            p.setBrush(QColor(cor) if i < corte else apagada)
            p.drawRoundedRect(QRect(r.left() + i * 5, r.center().y() - h // 2, 2, h), 1, 1)

    def _retrato_de_voz(self, p, r: QRect, msg: dict, meu: bool, det: dict):
        """Foto de quem gravou, à direita do cartão, com o selo de microfone —
        é o que o WhatsApp Web mostra em nota de voz."""
        if meu:
            nome, jid, caminho = self.eu
        elif self._em_grupo(msg):
            # em grupo cada mensagem tem seu autor: sem foto dele, iniciais
            jid = (msg.get("key") or {}).get("participant", "")
            nome, caminho = self._nome_autor(msg), ""
        else:
            nome, jid, caminho = self.contato
        pix = foto(caminho, r.width()) if caminho else QPixmap()
        if pix.isNull():
            pix = avatar(nome or "?", jid or "?", r.width())
        p.drawPixmap(r, pix)
        if not det.get("ptt"):
            return  # arquivo de música não ganha o selo, só a nota de voz
        lado = max(ESCALA.e(13), r.width() // 3)
        selo = QRect(r.left(), r.bottom() - lado + 1, lado, lado)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P["accent"]))
        p.drawEllipse(selo)
        p.setPen(QColor("#ffffff"))
        f = QFont()
        f.setPixelSize(max(8, int(lado * 0.62)))
        p.setFont(f)
        p.drawText(selo, Qt.AlignCenter, "🎤")

    @staticmethod
    def _selo_hd(p, area: QRect):
        """'HD' no canto superior direito da imagem — o que o WhatsApp mostra
        para imagem em alta qualidade (recebida via payload, nossa via mapa)."""
        rotulo = "HD"
        f = p.font()
        f.setPointSize(max(6, int(f.pointSizeF() * 0.62)) if f.pointSizeF() > 0 else 8)
        f.setBold(True)
        p.setFont(f)
        m = QFontMetrics(f)
        larg = m.horizontalAdvance(rotulo) + 10
        alt = m.height()
        pill = QRect(area.right() - larg - 5, area.top() + 5, larg, alt)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawRoundedRect(pill, alt // 2, alt // 2)
        p.setPen(QColor("#ffffff"))
        p.drawText(pill, Qt.AlignCenter, rotulo)

    def _pintar_audio(self, p, opt, caixa, y, det, msg, meu):
        mid = (msg.get("key") or {}).get("id", "")
        seg = det.get("segundos", 0)
        fracao = AUDIO.progresso(mid) if AUDIO else 0.0
        tocando = bool(AUDIO and AUDIO.tocando(mid))
        botao, onda, retrato = self.geometria_audio(caixa, y)
        self._retrato_de_voz(p, retrato, msg, meu, det)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_mix(P["meu"] if meu else P["dele"], P["texto"], 0.14)))
        p.drawEllipse(botao)
        p.setPen(QColor(P["texto"]))
        p.setFont(fonte(opt.font, 2))
        p.drawText(botao, Qt.AlignCenter, "⏸" if tocando else "▶")

        # a onda do payload vem com a mensagem e nao depende de download; os
        # picos do ffmpeg so' entram quando ela nao veio (audio nosso, antigo)
        picos = list(onda_do_payload(det["onda"])) if det.get("onda") else msg.get("_picos")
        self._onda(p, onda, P["hora_minha"] if meu else "#53bdeb",
                   sum(map(ord, mid or "x")), fracao, picos)
        if fracao:  # cabeçote de reprodução
            cx = onda.left() + int(onda.width() * fracao)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QPoint(cx, onda.center().y()), 4, 4)

        corrente = AUDIO.posicao_seg() if (AUDIO and AUDIO.mid == mid) else seg
        p.setFont(fonte(opt.font, -2.5))
        p.setPen(QColor(P["fraco_meu"] if meu else P["fraco_dele"]))
        p.drawText(QRect(onda.left(), y + 38, 70, 14), Qt.AlignLeft,
                   f"{corrente // 60}:{corrente % 60:02d}")

    def _sobrepor_video(self, p, opt, area: QRect, det: dict, animando: bool = False):
        """Botão de play no meio e duração no canto, como no WhatsApp Web.

        `animando`: o GIF já está rodando na bolha — o Web não põe ▶ em cima de
        conteúdo em movimento, só mantém o selo "GIF".
        """
        if animando:
            self._selo_video(p, opt, area, det)
            return
        raio = ESCALA.e(44)
        centro = QRect(area.center().x() - raio // 2, area.center().y() - raio // 2, raio, raio)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 130))
        p.drawEllipse(centro)
        p.setPen(QColor("#ffffff"))
        f = QFont()
        f.setPixelSize(ESCALA.e(18))
        p.setFont(f)
        p.drawText(centro, Qt.AlignCenter, "▶")

        self._selo_video(p, opt, area, det)

    def _selo_video(self, p, opt, area: QRect, det: dict):
        """Pílula do canto: duração do vídeo, ou "GIF"."""
        seg = det.get("segundos", 0)
        if not seg and not det.get("gif"):
            return
        # GIF não mostra duração: o selo é "GIF" (o Web também não marca tempo)
        gif = bool(det.get("gif"))
        rotulo = "GIF" if gif else f"{seg // 60}:{seg % 60:02d}"
        fp = fonte(opt.font, -2.5, negrito=gif)
        p.setFont(fp)
        larg = QFontMetrics(fp).horizontalAdvance(rotulo) + ESCALA.e(14)
        alt = ESCALA.e(18)
        pill = QRect(area.left() + 6, area.top() + 6, larg, alt)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 130))
        p.drawRoundedRect(pill, alt // 2, alt // 2)
        p.setPen(QColor("#ffffff"))
        p.drawText(pill, Qt.AlignCenter, ("" if gif else "🎬 ") + rotulo)

    def _pintar_documento(self, p, opt, caixa, y, det, meu=False):
        nome = det.get("nome", "documento")
        icone = QRect(caixa.left() + self.PAD, y + 4, 34, 40)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(cor_doc(nome)))
        p.drawRoundedRect(icone, 4, 4)
        p.setPen(QColor("#ffffff"))
        p.setFont(fonte(opt.font, -3, negrito=True))
        ext = (nome.rsplit(".", 1)[-1][:4].upper() if "." in nome else "DOC")
        p.drawText(icone, Qt.AlignCenter, ext)
        x = icone.right() + 10
        larg = caixa.right() - x - self.PAD
        p.setFont(fonte(opt.font, 0))
        p.setPen(QColor(P["texto"]))
        p.drawText(QRect(x, y + 6, larg, 18), Qt.AlignVCenter,
                   QFontMetrics(fonte(opt.font, 0)).elidedText(nome, Qt.ElideMiddle, larg))
        p.setFont(fonte(opt.font, -2.5))
        p.setPen(QColor(P["fraco_meu"] if meu else P["fraco_dele"]))
        detalhe = " • ".join(x for x in (ext, api.tamanho_humano(det.get("bytes", 0))) if x)
        p.drawText(QRect(x, y + 26, larg, 16), Qt.AlignVCenter, detalhe)

    def _pintar_local(self, p, opt, caixa, y, det, meu=False):
        mapa = QRect(caixa.left() + self.PAD, y + 2,
                     caixa.width() - self.PAD * 2, 76)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P["dele"]))
        p.drawRoundedRect(mapa, 6, 6)
        p.setPen(QPen(QColor(P["divisor"]), 1))
        for i in range(1, 4):  # grade discreta, so' para nao ser um bloco vazio
            p.drawLine(mapa.left(), mapa.top() + i * 19, mapa.right(), mapa.top() + i * 19)
            p.drawLine(mapa.left() + i * (mapa.width() // 4), mapa.top(),
                       mapa.left() + i * (mapa.width() // 4), mapa.bottom())
        p.setPen(QColor(P["perigo"]))
        p.setFont(fonte(opt.font, 6))
        p.drawText(mapa, Qt.AlignCenter, "📍")
        p.setFont(fonte(opt.font, -1))
        p.setPen(QColor(P["texto"]))
        p.drawText(QRect(mapa.left(), mapa.bottom() + 4, mapa.width(), 16),
                   Qt.AlignLeft, det.get("nome") or "Localização")
        p.setFont(fonte(opt.font, -2.5))
        p.setPen(QColor(P["fraco_meu"] if meu else P["fraco_dele"]))
        p.drawText(QRect(mapa.left(), mapa.bottom() + 20, mapa.width(), 14),
                   Qt.AlignLeft, det.get("endereco") or
                   f"{det.get('lat', 0):.4f}, {det.get('lon', 0):.4f}")

    def _pintar_contato(self, p, opt, caixa, y, det):
        nome = det.get("contato", "Contato")
        p.drawPixmap(caixa.left() + self.PAD, y + 4, avatar(nome, nome, 38))
        x = caixa.left() + self.PAD + 48
        p.setFont(fonte(opt.font, 0))
        p.setPen(QColor(P["texto"]))
        p.drawText(QRect(x, y + 8, caixa.right() - x - self.PAD, 20), Qt.AlignVCenter, nome)
        linha = y + 46
        p.setPen(QPen(QColor(P["divisor"]), 1))
        p.drawLine(caixa.left() + self.PAD, linha, caixa.right() - self.PAD, linha)
        p.setFont(fonte(opt.font, -1))
        p.setPen(QColor(P["citacao"]))
        p.drawText(QRect(caixa.left(), linha + 2, caixa.width(), 16), Qt.AlignCenter, "Conversar")

    def _pintar_link(self, p, opt, caixa, y, det, meu=False):
        """Cartão de link no formato do WhatsApp Web: imagem wide no topo
        (quando o payload traz a thumb embutida), depois domínio verde, título
        e descrição. Fundo = a própria bolha levemente saturada."""
        link = det["link"]
        fundo_cartao = _mix(P["meu"] if meu else P["dele"], P["texto"], 0.09)
        img_larg = int(link.get("_img_larg") or caixa.width() - self.PAD * 2)
        img_h = int(link.get("_img_h") or 0)
        faixa = QRect(caixa.left() + self.PAD, y + 2, img_larg, img_h + self.LINK - 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(fundo_cartao))
        p.drawRoundedRect(faixa, 6, 6)

        topo_texto = faixa.top()
        if img_h:
            thumb = link_pix(link["thumb"], img_larg)
            if not thumb.isNull():
                p.save()
                recorte = QPainterPath()
                recorte.addRoundedRect(
                    QRectF(faixa.left(), faixa.top(), img_larg, img_h), 6, 6
                )
                p.setClipPath(recorte)  # cantos da imagem seguem o cartão
                p.drawPixmap(faixa.left(), faixa.top(), thumb)
                p.restore()
            topo_texto = faixa.top() + img_h

        # domínio: verde do tema, afastado até AA contra o fundo do cartão
        p.setFont(fonte(opt.font, -2.5))
        cor_dom = _superficie(
            P["badge_texto"], fundo_cartao,
            "#000000" if _claro(fundo_cartao) else "#ffffff",
        )
        p.setPen(QColor(cor_dom))
        m_dom = QFontMetrics(fonte(opt.font, -2.5))
        dominio = dominio_de(link["url"])
        p.drawText(QRect(faixa.left() + 8, topo_texto + 1, faixa.width() - 16, 13),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   m_dom.elidedText(dominio, Qt.ElideRight, faixa.width() - 16))
        p.setFont(fonte(opt.font, -1, negrito=True))
        p.setPen(QColor(P["texto"]))
        m = QFontMetrics(fonte(opt.font, -1, negrito=True))
        p.drawText(QRect(faixa.left() + 8, topo_texto + 14, faixa.width() - 16, 18),
                   Qt.AlignVCenter, m.elidedText(link["titulo"] or link["url"],
                                                 Qt.ElideRight, faixa.width() - 16))
        p.setFont(fonte(opt.font, -2.5))
        p.setPen(QColor(P["fraco_meu"] if meu else P["fraco_dele"]))
        m2 = QFontMetrics(fonte(opt.font, -2.5))
        p.drawText(QRect(faixa.left() + 8, topo_texto + 33, faixa.width() - 16, 15),
                   Qt.AlignVCenter, m2.elidedText(link["descricao"] or link["url"],
                                                  Qt.ElideRight, faixa.width() - 16))

    # ---------- pintura ----------

    def paint(self, p, opt, ix):
        msg, texto, pix, det, larg, alto, mostra_texto = self._medir(opt, ix)
        meu = (msg.get("key") or {}).get("fromMe", False)
        topo = opt.rect.top()

        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if opt.state & QStyle.State_Selected:
            # a busca rola ate' a mensagem; sem isto o usuario tem de cacar a olho
            p.fillRect(opt.rect, QColor(P["selecao"]))

        if dia := self._vira_o_dia(ix):
            f = fonte(opt.font, -1.5)
            p.setFont(f)
            larg_dia = QFontMetrics(f).horizontalAdvance(dia) + 24
            pilula = QRect(opt.rect.center().x() - larg_dia // 2, topo + 6, larg_dia, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(P["dele"]))
            p.drawRoundedRect(pilula, 10, 10)
            p.setPen(QColor(P["fraco"]))
            p.drawText(pilula, Qt.AlignCenter, dia)
            topo += self.DIA

        # margem lateral proporcional: 5% em tela larga, piso para tiling estreito
        margem = max(ESCALA.e(8), int(opt.rect.width() * 0.05))
        recuo = self.RECUO if self._em_grupo(msg) else 0
        x = opt.rect.right() - larg - margem if meu else opt.rect.left() + margem + recuo
        respiro = ESCALA.e(2) if self._mesmo_autor(ix) else self.MARGEM
        caixa = QRect(x, topo + respiro // 2, larg, alto)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P["meu"] if meu else P["dele"]))
        p.drawRoundedRect(caixa, 8, 8)
        if msg.get("_realce"):
            # clicou na citação e pulou até aqui: o Web pisca a original por
            # alguns instantes para dizer "é esta". Véu por cima da bolha
            # inteira, que some sozinho (ver Janela._realcar).
            veu = QColor(P["accent"])
            veu.setAlpha(70)
            p.setBrush(veu)
            p.drawRoundedRect(caixa, 8, 8)

        if self._cabeca(ix, msg):
            part = (msg.get("key") or {}).get("participant", "")
            nome_autor = self._nome_autor(msg)
            lado = self.AVATAR
            retrato = foto(msg["_foto_autor"], lado) if msg.get("_foto_autor") else QPixmap()
            p.drawPixmap(
                opt.rect.left() + margem, caixa.top(),
                retrato if not retrato.isNull() else avatar(nome_autor, part, lado),
            )

        y = caixa.top() + self.PAD_V
        if self._cabeca(ix, msg):
            p.setFont(fonte(opt.font, -0.5, negrito=True))
            p.setPen(QColor(cor_autor((msg.get("key") or {}).get("participant", ""))))
            p.drawText(
                QRect(caixa.left() + self.PAD_H, y, larg - self.PAD_H * 2, self.AUTOR),
                Qt.AlignVCenter, self._nome_autor(msg),
            )
            y += self.AUTOR
        if citado := citacao_de(msg):
            faixa = QRect(caixa.left() + self.PAD, y, larg - self.PAD * 2, self.CITACAO - 6)
            p.setPen(Qt.NoPen)
            # ponytail: preto fixo empasta os 7 temas claros — um tom da propria
            # bolha escurece no escuro e satura no claro, sem token novo
            p.setBrush(QColor(_mix(P["meu"] if meu else P["dele"], P["texto"], 0.12)))
            p.drawRoundedRect(faixa, 4, 4)
            p.fillRect(QRect(faixa.left(), faixa.top(), 3, faixa.height()),
                       QColor(P["hora_minha"] if meu else P["citacao"]))
            f = fonte(opt.font, -2)
            p.setFont(f)
            direita = faixa.right()
            if citado[2]:  # a citada tinha mídia: mostra a miniatura dela
                lado = faixa.height() - 4
                mini = thumb_embutida(citado[2], lado)
                if not mini.isNull():
                    p.drawPixmap(faixa.right() - lado - 2, faixa.top() + 2,
                                 mini.scaled(lado, lado, Qt.KeepAspectRatioByExpanding,
                                             Qt.SmoothTransformation))
                    direita = faixa.right() - lado - 8
            util_txt = max(20, direita - faixa.left() - 14)
            p.setPen(QColor(P["citacao_bolha"] if meu else P["citacao"]))
            p.drawText(QRect(faixa.left() + 8, faixa.top() + 2, util_txt, 14),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(f).elidedText(citado[0], Qt.ElideRight, util_txt))
            p.setPen(QColor(P["fraco_meu"] if meu else P["fraco_dele"]))
            p.drawText(QRect(faixa.left() + 8, faixa.top() + 16, util_txt, 14),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(f).elidedText(citado[1], Qt.ElideRight, util_txt))
            p.setFont(opt.font)
            y += self.CITACAO

        cartao = self._cartao(det)
        if pix:
            area = QRect(caixa.left() + self.PAD, y, pix.width(), pix.height())
            # GIF toca sozinho na bolha, como no Web. O quadro entra no lugar da
            # miniatura e usa a MESMA area — a altura ja' foi medida pela thumb,
            # que sai do proprio video e tem a mesma proporcao.
            movimento = (
                ANIMA.quadro_de((msg.get("key") or {}).get("id", ""),
                                msg.get("_arquivo") or "")
                if (det.get("gif") and ANIMA) else None
            )
            if movimento is not None and not movimento.isNull():
                p.drawPixmap(area, movimento)
            else:
                p.drawPixmap(area.topLeft(), pix)
            if det.get("tipo") == "videoMessage":
                self._sobrepor_video(p, opt, area, det, animando=movimento is not None)
            if det.get("hd"):
                self._selo_hd(
                    p, QRect(caixa.left() + self.PAD, y, pix.width(), pix.height())
                )
            y += pix.height() + (6 if mostra_texto else 0)
        if cartao == "audio":
            self._pintar_audio(p, opt, caixa, y, det, msg, meu)
            y += self.CARTAO
        elif cartao == "documento":
            self._pintar_documento(p, opt, caixa, y, det, meu)
            y += self.CARTAO
        elif cartao == "local":
            self._pintar_local(p, opt, caixa, y, det, meu)
            y += 128
        elif cartao == "contato":
            self._pintar_contato(p, opt, caixa, y, det)
            y += self.CARTAO
        elif cartao == "link":
            self._pintar_link(p, opt, caixa, y, det, meu)
            y += int((det.get("link") or {}).get("_img_h") or 0) + self.LINK

        if mostra_texto:
            apagada = api.foi_apagada(msg)
            doc = documento(texto, larg - self.PAD * 2, ESCALA.px(),
                            P["fraco"] if apagada else P["texto"], apagada)
            p.save()
            p.translate(caixa.left() + self.PAD, y)
            doc.drawContents(p)
            p.restore()

        f_rodape = fonte(opt.font, -2.5)
        p.setFont(f_rodape)
        m = QFontMetrics(f_rodape)
        alt_rodape = m.height()
        base_rodape = (
            caixa.bottom() - self.PAD_V - alt_rodape
            - (self.REACAO if msg.get("_reacoes") else 0)
        )
        rotulo, marca, azul = self._rodape_de(msg)
        larg_marca = (m.horizontalAdvance(marca) + ESCALA.e(4)) if marca else 0
        p.setPen(QColor(P["hora_minha"] if meu else P["fraco_dele"]))
        p.drawText(
            QRect(caixa.left(), base_rodape, larg - self.PAD_H - larg_marca, alt_rodape),
            Qt.AlignRight, rotulo,
        )
        if marca:
            # ✓✓ azul so' quando lido/ouvido — o resto acompanha a cor da hora
            p.setPen(QColor(
                P["citacao_bolha"] if azul else (P["hora_minha"] if meu else P["fraco_dele"])
            ))
            p.drawText(
                QRect(caixa.right() - self.PAD_H - larg_marca, base_rodape,
                      larg_marca, alt_rodape),
                Qt.AlignRight, marca,
            )

        emojis, quantos = api.resumo_reacoes(msg)
        if emojis:
            resumo = emojis + (f" {quantos}" if quantos > 1 else "")
            f = fonte(opt.font, -1)
            p.setFont(f)
            larg_pill = QFontMetrics(f).horizontalAdvance(resumo) + 16
            pill = QRect(caixa.left() + self.PAD, caixa.bottom() - self.REACAO + 1,
                         larg_pill, self.REACAO - 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(P["painel"]))
            p.drawRoundedRect(pill, 10, 10)
            p.setPen(QColor(P["texto"]))
            p.drawText(pill, Qt.AlignCenter, resumo)

        # ➦ de encaminhar no hover — área própria fora da bolha, como o Web
        if (opt.state & QStyle.State_MouseOver) and not api.foi_apagada(msg):
            seta = self._area_seta(caixa, meu)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(P["painel"]))
            p.drawEllipse(seta)
            p.setPen(QColor(P["fraco"]))
            p.setFont(fonte(opt.font, 0))
            p.drawText(seta, Qt.AlignCenter, "➦")
        p.restore()


# --------------------------------------------------------------------------- composer


# --------------------------------------------------------------------------- emoji


# Ordem das abas do WhatsApp Web. O ícone é o emoji-símbolo da categoria.
GRUPOS_EMOJI = [
    ("sorrisos", "😀", "Sorrisos e pessoas"),
    ("pessoas", "👋", "Gestos e corpo"),
    ("animais", "🐶", "Animais e natureza"),
    ("comidas", "🍔", "Comidas e bebidas"),
    ("atividades", "⚽", "Atividades"),
    ("viagens", "✈️", "Viagens e lugares"),
    ("objetos", "💡", "Objetos"),
    ("simbolos", "🔤", "Símbolos"),
    ("bandeiras", "🚩", "Bandeiras"),
]


def _sem_acento(s: str) -> str:
    """'coração' -> 'coracao' — a busca ignora acentos, como o usuário digita."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.casefold())
        if unicodedata.category(c) != "Mn"
    )


def buscar_emojis(
    termo: str, grupo: str = "", recentes: list[str] | None = None
) -> list[tuple]:
    """Itens filtrados do catálogo: (char, nome_pt, keywords_pt, nome_en, grupo).

    Busca sem acento em nome/keywords PT + nome EN; recentes é a aba própria.
    """
    t = _sem_acento(termo.strip())
    if grupo == "__recentes__":
        mapa = {e[0]: e for e in emojis.EMOJIS}
        return [mapa[c] for c in (recentes or []) if c in mapa]
    if t:
        return [
            e for e in emojis.EMOJIS
            if t in _sem_acento(f"{e[1]} {e[2]} {e[3]}")
        ]
    if grupo:
        return [e for e in emojis.EMOJIS if e[4] == grupo]
    return list(emojis.EMOJIS)


class EmojiModel(QAbstractListModel):
    """Grade do seletor: cada item é uma tupla do catálogo emojis.py."""

    def __init__(self):
        super().__init__()
        self.itens: list[tuple] = []

    def rowCount(self, _=QModelIndex()):
        return len(self.itens)

    def data(self, ix, role=Qt.DisplayRole):
        e = self.itens[ix.row()]
        if role == Qt.DisplayRole:
            return e[0]
        if role == Qt.UserRole:
            return e
        if role == Qt.ToolTipRole:
            return e[1] or e[3]
        return None

    def definir(self, itens: list[tuple]):
        self.beginResetModel()
        self.itens = itens
        self.endResetModel()


class EmojiDelegate(QStyledItemDelegate):
    """Célula da grade: o emoji colorido (Noto Color Emoji) com hover/seleção."""

    def sizeHint(self, opt, ix):
        return QSize(ESCALA.e(38), ESCALA.e(38))

    def paint(self, p, opt, ix):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if opt.state & QStyle.State_Selected:
            p.fillRect(opt.rect, QColor(P["selecao"]))
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(opt.rect, QColor(P["hover"]))
        f = QFont()
        f.setFamilies(["Noto Color Emoji", "sans-serif"])
        f.setPixelSize(round(ESCALA.e(38) * 0.6))
        p.setFont(f)
        p.drawText(opt.rect, Qt.AlignCenter, ix.data(Qt.DisplayRole))
        p.restore()


class SeletorEmoji(QDialog):
    """Popup de emoji no padrão do WhatsApp Web: busca, categorias e recentes.

    Qt.Popup fecha sozinho ao clicar fora — sem overlay manual.
    """

    escolhido = Signal(str)

    def __init__(self, pai=None):
        super().__init__(pai, Qt.Popup)
        self.setObjectName("seletor_emoji")
        self.setStyleSheet(f"QDialog {{ background:{P['painel']}; }}")
        self.recentes = api.carregar_emojis_recentes()
        self.grupo = "__recentes__" if self.recentes else "sorrisos"
        self.modelo = EmojiModel()

        self.grade = QListView()
        self.grade.setModel(self.modelo)
        self.grade.setItemDelegate(EmojiDelegate())
        self.grade.setFlow(QListView.LeftToRight)
        self.grade.setWrapping(True)
        self.grade.setUniformItemSizes(True)
        self.grade.setSpacing(0)
        self.grade.setMouseTracking(True)
        self.grade.setStyleSheet(
            "QListView { background: transparent; border: none; outline: none; }"
        )
        self.grade.clicked.connect(self._escolher)

        self.busca = QLineEdit()
        self.busca.setPlaceholderText("Buscar emoji… (ex.: coração, foguete)")
        self.busca.setClearButtonEnabled(True)
        self.busca.textChanged.connect(self._aplicar)

        abas = QHBoxLayout()
        abas.setSpacing(ESCALA.e(4))
        self.botoes: dict[str, QPushButton] = {}
        ordem = [("__recentes__", "🕘", "Recentes")] + [
            (chave, icone, nome) for chave, icone, nome in GRUPOS_EMOJI
        ]
        for chave, icone, nome in ordem:
            b = QPushButton(icone)
            b.setCheckable(True)
            b.setProperty("abaFiltro", True)
            b.setToolTip(nome)
            b.setFixedHeight(ESCALA.e(30))
            b.clicked.connect(lambda _=False, k=chave: self._aba(k))
            self.botoes[chave] = b
            abas.addWidget(b)
        abas.addStretch(1)
        self.botoes["__recentes__"].setEnabled(bool(self.recentes))

        caixa = QVBoxLayout(self)
        caixa.setContentsMargins(
            ESCALA.e(10), ESCALA.e(8), ESCALA.e(10), ESCALA.e(8)
        )
        caixa.setSpacing(ESCALA.e(6))
        caixa.addWidget(self.busca)
        caixa.addLayout(abas)
        caixa.addWidget(self.grade, 1)
        self.setFixedSize(ESCALA.e(400), ESCALA.e(350))
        self._aplicar()

    def _aba(self, chave: str):
        self.grupo = chave
        self.busca.clear()
        for k, b in self.botoes.items():
            b.setChecked(k == chave)
        self._aplicar()

    def _aplicar(self, *_):
        termo = self.busca.text()
        if termo.strip():
            itens = buscar_emojis(termo)
        else:
            itens = buscar_emojis("", self.grupo, self.recentes)
        self.modelo.definir(itens)

    def _escolher(self, ix):
        e = ix.data(Qt.UserRole)
        if not e:
            return
        char = e[0]
        if char in self.recentes:
            self.recentes.remove(char)
        self.recentes.insert(0, char)
        api.salvar_emojis_recentes(self.recentes[:40])
        self.escolhido.emit(char)
        self.close()


# --------------------------------------------------------------------------- seletor GIF


class GifModel(QAbstractListModel):
    """Grade do seletor de GIF. Cada item é um result do Giphy; o preview chega
    em thread e vira um QMovie na GUI (result["_mv"]).

    Animado, como no Web: o preview do Giphy é um .gif de verdade, então o
    QMovie faz o loop e o tempo dos quadros sozinho — nada de decodificar vídeo
    aqui.
    """

    def __init__(self):
        super().__init__()
        self.itens: list[dict] = []

    def rowCount(self, _=QModelIndex()):
        return len(self.itens)

    def data(self, ix, role=Qt.DisplayRole):
        if role == Qt.UserRole:
            return self.itens[ix.row()]
        return None

    def definir(self, itens: list[dict]):
        self.beginResetModel()
        for velho in self.itens:  # busca nova: nada de animacao orfa gastando CPU
            if mv := velho.get("_mv"):
                mv.stop()
        self.itens = itens
        self.endResetModel()

    def preview_pronto(self, gid: str, caminho: str):
        for i, it in enumerate(self.itens):
            if it.get("id") != gid:
                continue
            ix = self.index(i)
            if not caminho:
                it["_pix"] = None  # falhou: não tenta de novo
                self.dataChanged.emit(ix, ix)
                return
            mv = QMovie(caminho)
            if mv.isValid() and mv.frameCount() != 1:
                it["_mv"] = mv
                # o repaint vem do quadro, nao do dataChanged: a celula tem
                # tamanho fixo, remedir a grade 15x/s por item seria desperdicio
                mv.frameChanged.connect(
                    lambda _n, ix=ix: self.dataChanged.emit(ix, ix)
                )
                mv.start()
            else:  # sem animacao (ou formato que o Qt nao anima): quadro parado
                it["_pix"] = QPixmap(caminho)
            self.dataChanged.emit(ix, ix)
            return


class GifDelegate(QStyledItemDelegate):
    """Célula da grade: preview centralizado com fundo neutro; seleção como
    o Web (borda). O preview é 16:9; a célula acompanha."""

    LARG = 176

    def sizeHint(self, opt, ix):
        return QSize(ESCALA.e(self.LARG), ESCALA.e(99))

    def paint(self, p, opt, ix):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        c = opt.rect
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(P["divisor"]))
        p.drawRoundedRect(c, 4, 4)
        it = ix.data(Qt.UserRole) or {}
        mv = it.get("_mv")
        pix: QPixmap | None = mv.currentPixmap() if mv is not None else it.get("_pix")
        if pix is not None and not pix.isNull():
            # cover: corta o excesso mantendo o centro
            alvo = c.adjusted(1, 1, -1, -1)
            esc = min(alvo.width() / max(1, pix.width()),
                      alvo.height() / max(1, pix.height()))
            origem = QRectF((pix.width() - alvo.width() / esc) / 2,
                            (pix.height() - alvo.height() / esc) / 2,
                            alvo.width() / esc, alvo.height() / esc)
            recorte = QPainterPath()
            recorte.addRoundedRect(QRectF(alvo), 3, 3)
            p.setClipPath(recorte)
            p.drawPixmap(QRectF(alvo), pix, origem)
            p.setClipping(False)
        elif it:  # ainda sem preview: placeholder discreto
            p.setPen(QColor(P["fraco"]))
            f = p.font(); f.setPixelSize(round(ESCALA.e(99) * 0.3))
            p.setFont(f)
            p.drawText(c, Qt.AlignCenter, "▮▮")
        if opt.state & QStyle.State_Selected:
            p.setPen(QPen(QColor(P["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(c.adjusted(1, 1, -1, -1), 4, 4)
        p.restore()


class SeletorGif(QDialog):
    """Popup de GIF estilo WhatsApp Web: busca (Giphy) + trending + grade.

    Mesma biblioteca que o WhatsApp Web usa. A key (grátis) do Giphy vai no ⚙.

    A atribuição "Powered by GIPHY" na barra NÃO é decoração: os Termos da API
    exigem exibi-la de forma visível onde a API é usada.
    Clicar num GIF emite `escolhido(result)` — a Janela baixa o mp4 e abre o
    diálogo de envio, que confirma antes de mandar.
    """

    escolhido = Signal(dict)
    _prontos = Signal(str, object, object)  # (termo, results|None, erro|None)
    _preview_ok = Signal(str, str)  # (id do result, caminho|"")

    def __init__(self, pai=None):
        super().__init__(pai, Qt.Popup)
        self.setObjectName("seletor_gif")
        self.setStyleSheet(f"QDialog {{ background:{P['painel']}; }}")
        self._janela = pai
        self._chave = str((pai.cfg or {}).get("giphy_apikey") or "") if pai else ""
        self.modelo = GifModel()
        self._prontos.connect(self._receber_prontos)
        self._preview_ok.connect(self._receber_preview)

        self.grade = QListView()
        self.grade.setModel(self.modelo)
        self.grade.setItemDelegate(GifDelegate())
        self.grade.setFlow(QListView.LeftToRight)
        self.grade.setWrapping(True)
        self.grade.setUniformItemSizes(True)
        self.grade.setSpacing(0)
        self.grade.setMouseTracking(True)
        self.grade.setStyleSheet(
            "QListView { background: transparent; border: none; outline: none; }"
        )
        self.grade.clicked.connect(self._escolher)

        self.busca = QLineEdit()
        self.busca.setPlaceholderText("Pesquisar GIFs… (ex.: bom dia)")
        self.busca.setClearButtonEnabled(True)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._buscar)

        self.estado = QLabel("")
        self.estado.setStyleSheet(
            f"color:{P['fraco']}; background:transparent; font-size:{ESCALA.px() - 3}px;"
        )
        self.estado.setWordWrap(True)
        self.btn_config = QPushButton("⚙  Abrir Configuração (cola a key do Giphy)")
        self.btn_config.clicked.connect(self._configurar)
        self.btn_config.setVisible(False)

        caixa = QVBoxLayout(self)
        caixa.setContentsMargins(ESCALA.e(10), ESCALA.e(8), ESCALA.e(10), ESCALA.e(8))
        caixa.setSpacing(ESCALA.e(6))
        caixa.addWidget(self.busca)
        caixa.addWidget(self.grade, 1)
        caixa.addWidget(self.estado)
        # Exigencia dos Termos da API do Giphy: a atribuicao tem de ficar
        # visivel onde a API e' usada. Nao e' enfeite, e' contrato.
        creditos = QLabel("Powered by GIPHY")
        creditos.setAlignment(Qt.AlignRight)
        creditos.setStyleSheet(
            f"color:{P['fraco']}; background:transparent; font-size:{ESCALA.px() - 4}px;"
        )
        caixa.addWidget(creditos)
        caixa.addWidget(self.btn_config)
        self.setFixedSize(ESCALA.e(400), ESCALA.e(380))
        self.busca.textChanged.connect(lambda _t: self._debounce.start(350))
        self._iniciar()

    def _configurar(self):
        janela = self._janela
        self.close()
        if janela:
            janela.abrir_config()

    def _iniciar(self):
        self._podar_cache()  # o cache de GIFs baixados fica só com os 80 novos
        self._buscar()

    def _podar_cache(self):
        try:
            arquivos = sorted(api.GIFS.glob("gif_*.mp4"),
                              key=lambda f: f.stat().st_mtime)
            for f in arquivos[:-80]:
                f.unlink(missing_ok=True)
        except Exception:
            pass

    def _buscar(self):
        termo = self.busca.text().strip()
        if not self._chave:
            self.modelo.definir([])
            self.estado.setText(
                "Para buscar GIFs: crie uma key grátis em developers.giphy.com "
                "e cole no ⚙ Configuração (menu do seu perfil)."
            )
            self.btn_config.setVisible(True)
            return
        self.btn_config.setVisible(False)
        self.estado.setText("buscando…" if termo else "carregando GIFs populares…")

        def trabalho():
            try:
                results = api.giphy_buscar(termo, self._chave)
                self._prontos.emit(termo, results, None)
            except Exception as e:
                msg = str(e)
                if getattr(e, "response", None) is not None:
                    msg = (
                        "key do Giphy inválida ou cota da hora estourada (100/h) "
                        f"(HTTP {getattr(e.response, 'status_code', '?')})"
                    )
                self._prontos.emit(termo, None, msg)

        POOL.submit(trabalho)

    def _receber_prontos(self, termo, results, erro):
        if erro:
            self.estado.setText(erro)
            self.btn_config.setVisible(True)
            self.modelo.definir([])
            return
        self.modelo.definir(results or [])
        self.estado.setText("" if results else "nada encontrado — tente outro termo")
        # previews em paralelo. Todos, nao so' os visiveis: a grade rola e o
        # preview do Giphy e' de 100px — os 24 juntos dao ~1 MB.
        for it in (results or []):
            prev = it.get("prev") or it.get("mp4")
            if not prev:
                continue

            def puxar(gid, url):
                caminho = api.gif_baixar(url)
                self._preview_ok.emit(gid, str(caminho) if caminho else "")

            POOL.submit(puxar, it["id"], prev)

    def _receber_preview(self, gid, caminho):
        self.modelo.preview_pronto(gid, caminho)

    def _escolher(self, ix):
        it = ix.data(Qt.UserRole)
        if it:
            self.escolhido.emit(it)
            self.close()

    def closeEvent(self, e):
        """Popup fechado não anima: sem isto os QMovie seguiriam rodando."""
        self.modelo.definir([])
        super().closeEvent(e)


# --------------------------------------------------------------------------- composer


class Composer(QTextEdit):
    """Campo de texto que também aceita arquivo por Ctrl+V e por arrastar."""

    enviar = Signal(str)
    arquivos = Signal(list)
    editar_ultima = Signal()

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("Escreva uma mensagem — Ctrl+V cola arquivo")
        self.setAcceptDrops(True)
        # ponytail: altura por LINHAS, não fixa — a caixa nasce com uma linha e
        # cresce até 5, como no WhatsApp. Antes ocupava 110px sempre.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.document().documentLayout().documentSizeChanged.connect(self._ajustar)
        self._ajustar()

    def _ajustar(self, *_):
        linha = QFontMetrics(self.font()).lineSpacing()
        preciso = int(self.document().size().height()) + linha // 2
        self.setFixedHeight(max(linha + ESCALA.e(16), min(preciso + ESCALA.e(12), linha * 5)))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            if texto := self.toPlainText().strip():
                self.enviar.emit(texto)
                self.clear()
            return
        # ↑ com o campo VAZIO edita a última mensagem enviada. É nativo no Web
        # (existia uma extensão para isso; foi descontinuada quando o WhatsApp
        # passou a fazer sozinho). Com texto digitado, ↑ anda o cursor normal.
        if e.key() == Qt.Key_Up and not self.toPlainText() and not e.modifiers():
            self.editar_ultima.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, md):
        if caminhos := self._extrair(md):
            self.arquivos.emit(caminhos)
            return
        super().insertFromMimeData(md)

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        if caminhos := self._extrair(e.mimeData()):
            self.arquivos.emit(caminhos)
            e.acceptProposedAction()
            return
        super().dropEvent(e)

    def _extrair(self, md) -> list[pathlib.Path]:
        if md.hasUrls():
            locais = [pathlib.Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()]
            return [c for c in locais if c.is_file()]
        if md.hasImage():
            # print de tela colado: vira arquivo antes de subir
            destino = api.MEDIA / f"colado-{datetime.now():%Y%m%d-%H%M%S}.png"
            md.imageData().save(str(destino), "PNG")
            return [destino]
        return []


# --------------------------------------------------------------------------- socket


class Eventos(QObject):
    """Socket.IO no namespace da instância. Os handlers rodam em thread do
    socketio; emitir Signal daqui vira conexão enfileirada no Qt sozinho."""

    mensagem = Signal(dict)
    apagou = Signal(dict)
    atualizou = Signal(str, str, str)
    presenca = Signal(str, str)
    chats_mudaram = Signal(list)
    conexao = Signal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.sio = socketio.Client(reconnection=True, reconnection_delay_max=30)
        ns = f"/{cfg['instance']}"
        self.sio.on("messages.upsert", self._msg, namespace=ns)
        self.sio.on("send.message", self._msg, namespace=ns)
        self.sio.on("messages.update", self._atualizada, namespace=ns)
        self.sio.on("messages.delete", self._apagada, namespace=ns)
        self.sio.on("presence.update", self._presenca, namespace=ns)
        for ev in ("chats.upsert", "chats.update", "chats.set"):
            self.sio.on(ev, self._chats, namespace=ns)
        self.sio.on("connection.update", self._conn, namespace=ns)
        # Sem estes dois o socket cai e o app continua parecendo online: o
        # python-socketio reconecta sozinho e em silencio, e enquanto nao
        # consegue nao chega mensagem nenhuma. Para um cliente de conversa e'
        # o pior modo de falha — quem olha a tela conclui que ninguem escreveu.
        self.sio.on("connect", lambda: self.conexao.emit("open"), namespace=ns)
        self.sio.on("disconnect", lambda: self.conexao.emit("caiu"), namespace=ns)

    def _msg(self, payload):
        if isinstance(d := payload.get("data"), dict) and d.get("key"):
            self.mensagem.emit(d)

    def _atualizada(self, payload):
        """Confirmação de entrega/leitura chegando: ✓ → ✓✓ → ✓✓ azul."""
        d = payload.get("data")
        for x in (d if isinstance(d, list) else [d]):
            if not isinstance(x, dict):
                continue
            mid = x.get("keyId") or (x.get("key") or {}).get("id")
            if mid and x.get("status"):
                self.atualizou.emit(mid, x["status"], x.get("remoteJid", ""))

    def _apagada(self, payload):
        if isinstance(d := payload.get("data"), dict) and d.get("key"):
            d.setdefault("key", {})["deleted"] = True
            self.apagou.emit(d)

    def _chats(self, payload):
        d = payload.get("data")
        itens = d if isinstance(d, list) else [d]
        limpos = [x for x in itens if isinstance(x, dict)]
        if limpos:
            self.chats_mudaram.emit(limpos)

    def _presenca(self, payload):
        d = payload.get("data") or {}
        jid = d.get("id") or ""
        estados = (d.get("presences") or {}).values()
        atual = next(iter(estados), {}).get("lastKnownPresence", "")
        if jid and atual:
            self.presenca.emit(jid, atual)

    def _conn(self, payload):
        self.conexao.emit((payload.get("data") or {}).get("state", "?"))

    def ligar(self):
        ns = f"/{self.cfg['instance']}"
        try:
            self.sio.connect(
                f"{self.cfg['host']}?apikey={self.cfg['apikey']}",
                namespaces=[ns],
                transports=["websocket"],
                wait_timeout=10,
            )
            self.conexao.emit("open")
        except Exception as e:
            log.warning("socket.io não conectou: %s", e)
            self.conexao.emit("offline")


def notificar(titulo: str, corpo: str):
    """ponytail: QtDBus ja vem no qt6-base. Nada de lib de notificacao."""
    iface = QDBusInterface(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        QDBusConnection.sessionBus(),
    )
    if not iface.isValid():
        return
    # ponytail: Notify tem 8 parametros; call() so aceita ate 4. E' esta a via.
    iface.callWithArgumentList(
        QDBus.NoBlock,
        "Notify",
        ["WhatsApp", 0, "", titulo, corpo[:160], [], {}, 5000],
    )


# --------------------------------------------------------------------------- audio


class AudioInline(QObject):
    """Um player só para toda a conversa — o WhatsApp também toca um áudio por
    vez. A bolha desenha o progresso; ninguém abre janela para ouvir."""

    mudou = Signal()

    def __init__(self, pai=None):
        super().__init__(pai)
        self.player = QMediaPlayer(self)
        self.saida = QAudioOutput(self)
        self.player.setAudioOutput(self.saida)
        self.saida.setVolume(0.9)
        self.mid = ""          # id da mensagem tocando
        self.rotulo = ""       # quem enviou, para o mini-player
        self.velocidade = 1.0
        self._seek_pendente: float | None = None
        self.player.durationChanged.connect(self._aplicar_seek)
        self.player.positionChanged.connect(lambda _: self.mudou.emit())
        self.player.playbackStateChanged.connect(lambda _: self.mudou.emit())
        self.player.mediaStatusChanged.connect(self._fim)

    def _fim(self, estado):
        # ponytail: no fim o WhatsApp deixa a onda cheia e o botao em ▶ — nao
        # some com o audio. Por isso mantemos o mid; so' a reproducao para.
        if estado == QMediaPlayer.EndOfMedia:
            self.mudou.emit()

    def _aplicar_seek(self, duracao):
        """setPosition antes de a mídia carregar é ignorado; guardamos e aplicamos."""
        if self._seek_pendente is not None and duracao:
            self.player.setPosition(int(duracao * self._seek_pendente))
            self._seek_pendente = None
            self.mudou.emit()

    def tocando(self, mid: str = "") -> bool:
        ativo = self.player.playbackState() == QMediaPlayer.PlayingState
        return ativo and (not mid or mid == self.mid)

    def progresso(self, mid: str) -> float:
        if mid != self.mid or not self.player.duration():
            return 0.0
        return self.player.position() / self.player.duration()

    def posicao_seg(self) -> int:
        return self.player.position() // 1000

    def alternar(self, mid: str, caminho: str, rotulo: str = ""):
        if mid == self.mid and self.player.source().isValid():
            if self.tocando(mid):
                self.player.pause()
            else:
                if self.player.position() >= max(0, self.player.duration() - 60):
                    self.player.setPosition(0)  # terminou: recomeça
                self.player.play()
            self.mudou.emit()
            return
        self.mid, self.rotulo = mid, rotulo
        self.player.setSource(QUrl.fromLocalFile(caminho))
        self.player.setPlaybackRate(self.velocidade)
        self.player.play()
        self.mudou.emit()

    def buscar(self, mid: str, fracao: float):
        fracao = max(0.0, min(1.0, fracao))
        if mid != self.mid:
            return
        if self.player.duration():
            self.player.setPosition(int(self.player.duration() * fracao))
            self.mudou.emit()
        else:
            self._seek_pendente = fracao  # ainda carregando

    def parar(self):
        self.player.stop()
        self.mid = ""
        self._seek_pendente = None
        self.mudou.emit()

    def trocar_velocidade(self) -> float:
        self.velocidade = {1.0: 1.5, 1.5: 2.0, 2.0: 1.0}[self.velocidade]
        self.player.setPlaybackRate(self.velocidade)
        self.mudou.emit()
        return self.velocidade


class MiniPlayer(QWidget):
    """Faixa no rodapé da barra lateral com o áudio em reprodução."""

    def __init__(self, audio: AudioInline, pai=None):
        super().__init__(pai)
        self.audio = audio
        self.setVisible(False)
        self.b_play = QPushButton("⏸")
        self.b_play.setFixedWidth(34)
        self.b_play.clicked.connect(
            lambda: audio.alternar(audio.mid, "", "") if audio.mid else None
        )
        self.rotulo = QLabel("")
        self.barra = QProgressBar()
        self.barra.setTextVisible(False)
        self.barra.setFixedHeight(4)
        self.b_vel = QPushButton("1×")
        self.b_vel.setFixedWidth(34)
        self.b_vel.clicked.connect(lambda: self.b_vel.setText(f"{audio.trocar_velocidade():g}×"))
        fechar = QPushButton("✕")
        fechar.setFixedWidth(28)
        fechar.clicked.connect(audio.parar)

        linha = QHBoxLayout()
        linha.setContentsMargins(8, 4, 8, 4)
        linha.setSpacing(6)
        linha.addWidget(self.b_play)
        coluna = QVBoxLayout()
        coluna.setSpacing(2)
        coluna.addWidget(self.rotulo)
        coluna.addWidget(self.barra)
        linha.addLayout(coluna, 1)
        linha.addWidget(self.b_vel)
        linha.addWidget(fechar)
        self.setLayout(linha)
        self.reestilizar()
        audio.mudou.connect(self.atualizar)

    def reestilizar(self):
        self.setStyleSheet(
            f"background:{P['selecao']}; border-top:1px solid {P['divisor']};"
        )
        self.rotulo.setStyleSheet(
            f"background:transparent; font-size:{ESCALA.px() - 2}px; color:{P['texto']};"
        )
        self.barra.setStyleSheet(
            f"QProgressBar {{ background:{P['divisor']}; border:none; border-radius:2px; }}"
            f"QProgressBar::chunk {{ background:{P['badge']}; border-radius:2px; }}"
        )

    def atualizar(self):
        if not self.audio.mid:
            self.setVisible(False)
            return
        self.setVisible(True)
        dur = self.audio.player.duration() or 1
        pos = self.audio.player.position()
        self.barra.setValue(int(pos * 100 / dur))
        self.barra.setMaximum(100)
        seg = pos // 1000
        total = dur // 1000
        self.rotulo.setText(
            f"🎤 {self.audio.rotulo}  ·  {seg // 60}:{seg % 60:02d} / {total // 60}:{total % 60:02d}"
        )
        self.b_play.setText("⏸" if self.audio.tocando() else "▶")


# --------------------------------------------------------------------------- captura


class Gravador(QObject):
    """PTT. Grava WAV — a Evolution converte para ogg/opus sozinha (verificado)."""

    pronto = Signal(pathlib.Path)

    def __init__(self, pai=None):
        super().__init__(pai)
        self.sessao = QMediaCaptureSession()
        self.entrada = QAudioInput()
        self.sessao.setAudioInput(self.entrada)
        self.rec = QMediaRecorder()
        self.sessao.setRecorder(self.rec)
        formato = QMediaFormat(QMediaFormat.Wave)
        formato.setAudioCodec(QMediaFormat.AudioCodec.Wave)
        self.rec.setMediaFormat(formato)
        self.rec.setQuality(QMediaRecorder.HighQuality)
        self.destino: pathlib.Path | None = None
        self.rec.recorderStateChanged.connect(self._estado)

    def gravando(self) -> bool:
        return self.rec.recorderState() == QMediaRecorder.RecordingState

    def iniciar(self):
        self.destino = api.MEDIA / f"ptt-{datetime.now():%Y%m%d-%H%M%S}.wav"
        self.rec.setOutputLocation(QUrl.fromLocalFile(str(self.destino)))
        self.rec.record()

    def parar(self):
        if self.gravando():
            self.rec.stop()

    def _estado(self, estado):
        if estado == QMediaRecorder.StoppedState and self.destino:
            alvo = pathlib.Path(self.rec.actualLocation().toLocalFile() or self.destino)
            QTimer.singleShot(350, lambda: self._entregar(alvo))

    def _entregar(self, alvo: pathlib.Path):
        if alvo.exists() and alvo.stat().st_size > 1024:
            self.pronto.emit(alvo)
        self.destino = None

    def duracao(self) -> int:
        return self.rec.duration()


class DialogoCamera(QDialog):
    """Câmera: foto ou vídeo, com escolha de dispositivo. Devolve o arquivo."""

    def __init__(self, pai=None):
        super().__init__(pai)
        self.setWindowTitle("Câmera")
        self.resize(760, 620)
        self.resultado: pathlib.Path | None = None
        self.como_recado = False

        self.sessao = QMediaCaptureSession()
        self.visor = QVideoWidget()
        self.sessao.setVideoOutput(self.visor)

        self.camera: QCamera | None = None
        self.foto = QImageCapture()
        self.sessao.setImageCapture(self.foto)
        self.foto.imageSaved.connect(self._foto_salva)

        self.audio = QAudioInput()
        self.sessao.setAudioInput(self.audio)
        self.rec = QMediaRecorder()
        self.sessao.setRecorder(self.rec)
        self.rec.recorderStateChanged.connect(self._estado_video)

        self.dispositivos = QComboBox()
        for d in QMediaDevices.videoInputs():
            self.dispositivos.addItem(d.description(), d)
        self.dispositivos.currentIndexChanged.connect(self._trocar_camera)

        self.b_foto = QPushButton("📷  Tirar foto")
        self.b_foto.clicked.connect(self.tirar_foto)
        self.b_video = QPushButton("⏺  Gravar vídeo")
        self.b_video.clicked.connect(self.alternar_video)
        self.recado = QCheckBox("Vídeo-recado (redondo)")
        self.contador = QLabel("")

        barra = QHBoxLayout()
        barra.addWidget(self.dispositivos, 1)
        barra.addWidget(self.recado)
        barra.addWidget(self.contador)
        barra.addWidget(self.b_foto)
        barra.addWidget(self.b_video)

        caixa = QVBoxLayout(self)
        caixa.addWidget(self.visor, 1)
        caixa.addLayout(barra)

        self.relogio = QTimer(self)
        self.relogio.timeout.connect(
            lambda: self.contador.setText(f"{self.rec.duration() // 1000}s")
        )
        if self.dispositivos.count():
            self._trocar_camera(0)
        else:
            self.contador.setText("nenhuma câmera encontrada")
            self.b_foto.setEnabled(False)
            self.b_video.setEnabled(False)

    def _trocar_camera(self, _):
        if self.camera:
            self.camera.stop()
        dev = self.dispositivos.currentData()
        if dev is None:
            return
        self.camera = QCamera(dev)
        self.sessao.setCamera(self.camera)
        self.camera.start()

    def tirar_foto(self):
        destino = api.MEDIA / f"foto-{datetime.now():%Y%m%d-%H%M%S}.jpg"
        self.foto.captureToFile(str(destino))

    def _foto_salva(self, _id, caminho):
        self.resultado = pathlib.Path(caminho)
        self.como_recado = False
        self.accept()

    def alternar_video(self):
        if self.rec.recorderState() == QMediaRecorder.RecordingState:
            self.rec.stop()
            return
        destino = api.MEDIA / f"video-{datetime.now():%Y%m%d-%H%M%S}.mp4"
        formato = QMediaFormat(QMediaFormat.MPEG4)
        formato.setVideoCodec(QMediaFormat.VideoCodec.H264)
        formato.setAudioCodec(QMediaFormat.AudioCodec.AAC)
        self.rec.setMediaFormat(formato)
        self.rec.setOutputLocation(QUrl.fromLocalFile(str(destino)))
        self.rec.record()
        self.b_video.setText("⏹  Parar")
        self.relogio.start(500)

    def _estado_video(self, estado):
        if estado != QMediaRecorder.StoppedState:
            return
        self.relogio.stop()
        self.b_video.setText("⏺  Gravar vídeo")
        arquivo = pathlib.Path(self.rec.actualLocation().toLocalFile() or "")
        if arquivo.exists() and arquivo.stat().st_size > 2048:
            self.resultado = arquivo
            self.como_recado = self.recado.isChecked()
            QTimer.singleShot(300, self.accept)

    def closeEvent(self, e):
        if self.camera:
            self.camera.stop()
        super().closeEvent(e)


# --------------------------------------------------------------------------- visualizador


REACOES = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".3gp")
AUDIO_EXT = (".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac")
IMAGEM_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
# ponytail: o resto (pdf, docx, zip) abre no app do sistema. O WhatsApp Web
# tambem nao faz preview de documento, e escrever visualizador de PDF nao
# e' este projeto.


class ZoomImagem(QLabel):
    """Imagem com zoom pela roda e arrasto para mover, como no WhatsApp Web."""

    def __init__(self):
        super().__init__()
        self.pix = QPixmap()
        self.escala = 1.0
        self.deslocamento = QPoint(0, 0)
        self._arrastando = None
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)

    def carregar(self, caminho: str):
        self.pix = QPixmap(caminho)
        self.escala, self.deslocamento = 1.0, QPoint(0, 0)
        self.update()

    def wheelEvent(self, e):
        if self.pix.isNull():
            return
        passo = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.escala = max(0.2, min(8.0, self.escala * passo))
        if self.escala <= 1.0:
            self.deslocamento = QPoint(0, 0)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.escala > 1.0:
            self._arrastando = e.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._arrastando is not None:
            atual = e.position().toPoint()
            self.deslocamento += atual - self._arrastando
            self._arrastando = atual
            self.update()

    def mouseReleaseEvent(self, _):
        self._arrastando = None
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, _):
        self.escala = 1.0 if self.escala > 1.0 else 2.5
        self.deslocamento = QPoint(0, 0)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 0))
        if self.pix.isNull():
            p.setPen(QColor(P["fraco"]))
            p.drawText(self.rect(), Qt.AlignCenter, "não foi possível abrir")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        base = min(self.width() / self.pix.width(), self.height() / self.pix.height(), 1.0)
        escala = base * self.escala
        w, h = int(self.pix.width() * escala), int(self.pix.height() * escala)
        x = (self.width() - w) // 2 + self.deslocamento.x()
        y = (self.height() - h) // 2 + self.deslocamento.y()
        p.drawPixmap(QRect(x, y, w, h), self.pix)


class PlayerMidia(QWidget):
    """Player embutido: vídeo com controles, ou só a barra quando é áudio."""

    def __init__(self):
        super().__init__()
        self.player = QMediaPlayer(self)
        self.saida = QAudioOutput(self)
        self.player.setAudioOutput(self.saida)
        self.saida.setVolume(0.9)

        self.tela = QVideoWidget()
        self.player.setVideoOutput(self.tela)

        self.play = QPushButton("⏸")
        self.play.setFixedWidth(40)
        self.play.clicked.connect(self.alternar)

        self.barra = QSlider(Qt.Horizontal)
        self.barra.sliderMoved.connect(self.player.setPosition)
        self.tempo = QLabel("0:00 / 0:00")
        self.tempo.setStyleSheet("background:transparent;")

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setFixedWidth(90)
        self.volume.setRange(0, 100)
        self.volume.setValue(90)
        self.volume.valueChanged.connect(lambda v: self.saida.setVolume(v / 100))

        controles = QHBoxLayout()
        controles.addWidget(self.play)
        controles.addWidget(self.barra, 1)
        controles.addWidget(self.tempo)
        controles.addWidget(QLabel("🔊"))
        controles.addWidget(self.volume)

        caixa = QVBoxLayout(self)
        caixa.setContentsMargins(0, 0, 0, 0)
        caixa.addWidget(self.tela, 1)
        caixa.addLayout(controles)

        self.player.positionChanged.connect(self._posicao)
        self.player.durationChanged.connect(self.barra.setMaximum)
        self.player.playbackStateChanged.connect(
            lambda s: self.play.setText("⏸" if s == QMediaPlayer.PlayingState else "▶")
        )
        self.player.mediaStatusChanged.connect(self._fim_loop)
        self._loop = False

    def _fim_loop(self, estado):
        """GIF é loop infinito: no fim, recomeça do zero (como o WhatsApp)."""
        if estado == QMediaPlayer.EndOfMedia and self._loop:
            self.player.setPosition(0)
            self.player.play()

    @staticmethod
    def _mmss(ms: int) -> str:
        s = max(0, ms) // 1000
        return f"{s // 60}:{s % 60:02d}"

    def _posicao(self, ms):
        if not self.barra.isSliderDown():
            self.barra.setValue(ms)
        self.tempo.setText(f"{self._mmss(ms)} / {self._mmss(self.player.duration())}")

    def alternar(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def abrir(self, caminho: str, com_video: bool, loop: bool = False):
        self._loop = loop
        self.tela.setVisible(com_video)
        self.player.setSource(QUrl.fromLocalFile(caminho))
        self.player.play()

    def parar(self):
        self.player.stop()
        self.player.setSource(QUrl())


class Visualizador(QDialog):
    """Lightbox do WhatsApp Web: mídia em tela cheia, navegação entre as do
    chat, zoom, player embutido, reação rápida e download."""

    reagiu = Signal(dict, str)

    def __init__(self, midias: list[dict], indice: int, titulo: str, pai=None,
                 menu_de=None):
        super().__init__(pai)
        self.midias = midias
        self.i = indice
        # fabrica do menu da mensagem (Janela.menu_para_mensagem). Quando vem
        # None — foto de perfil, que nao e' mensagem de verdade — o visualizador
        # fica so' com salvar/copiar, sem encaminhar nem apagar um id que nao existe.
        self.menu_de = menu_de
        self.setWindowTitle("Mídia")
        self.setModal(True)
        # sem isto, cada mídia aberta (inclusive as da galeria-de-1 do painel
        # de mídia) ficava pendurada pelo resto da sessão — só o hide() do
        # accept/reject/Esc, nunca destruído (achado do pente fino cruzado
        # com hermes/deepseek)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(1100, 780)
        self.setStyleSheet(
            f"QDialog {{ background:{_mix(P['fundo'], '#000000', 0.4)}; }}"
            f"QLabel {{ color:{P['texto']}; }}"
        )

        self.cabecalho = QLabel()
        self.cabecalho.setStyleSheet("background:transparent; font-size:14px; padding:4px 8px;")

        baixar = QPushButton("⤓")
        baixar.setToolTip("Salvar em Downloads")
        baixar.clicked.connect(self.salvar)
        self.favoritar = QPushButton("☆")
        self.favoritar.setFixedWidth(30)
        self.favoritar.setToolTip("Favoritar")
        self.favoritar.clicked.connect(lambda: self._disparar("☆", "★"))
        self.encaminhar = QPushButton("➦")
        self.encaminhar.setFixedWidth(30)
        self.encaminhar.setToolTip("Encaminhar")
        self.encaminhar.clicked.connect(lambda: self._disparar("➦"))
        self.mais = QPushButton("⋮")
        self.mais.setFixedWidth(30)
        self.mais.setToolTip("Mais ações")
        self.mais.clicked.connect(
            lambda: self._menu(self.mais.mapToGlobal(self.mais.rect().bottomLeft()))
        )
        fechar = QPushButton("✕")
        fechar.clicked.connect(self.accept)

        topo = QHBoxLayout()
        topo.addWidget(self.cabecalho, 1)
        for emoji in REACOES:
            b = QPushButton(emoji)
            b.setFixedWidth(38)
            b.setToolTip(f"Reagir com {emoji}")
            b.clicked.connect(lambda _=False, e=emoji: self._reagir(e))
            topo.addWidget(b)
        topo.addWidget(self.favoritar)
        topo.addWidget(baixar)
        topo.addWidget(self.encaminhar)
        topo.addWidget(self.mais)
        topo.addWidget(fechar)

        self.imagem = ZoomImagem()
        self.player = PlayerMidia()
        self.palco = QStackedWidget()
        self.palco.addWidget(self.imagem)
        self.palco.addWidget(self.player)

        self.anterior = QPushButton("‹")
        self.anterior.setFixedWidth(46)
        self.anterior.clicked.connect(lambda: self.ir(-1))
        self.proximo = QPushButton("›")
        self.proximo.setFixedWidth(46)
        self.proximo.clicked.connect(lambda: self.ir(1))
        for b in (self.anterior, self.proximo):
            b.setStyleSheet("font-size:30px; background:transparent;")

        meio = QHBoxLayout()
        meio.addWidget(self.anterior)
        meio.addWidget(self.palco, 1)
        meio.addWidget(self.proximo)

        self.legenda = QLabel()
        self.legenda.setWordWrap(True)
        self.legenda.setAlignment(Qt.AlignCenter)
        self.legenda.setStyleSheet(f"background:transparent; color:{P['texto']}; padding:6px;")

        caixa = QVBoxLayout(self)
        caixa.addLayout(topo)
        caixa.addLayout(meio, 1)
        caixa.addWidget(self.legenda)
        self.mostrar()

    def _atual(self) -> dict:
        return self.midias[self.i]

    def _caminho(self) -> str:
        return self._atual().get("_arquivo") or ""

    def mostrar(self):
        msg = self._atual()
        caminho = self._caminho()
        ext = pathlib.Path(caminho).suffix.lower()
        self.player.parar()

        if ext in IMAGEM_EXT:
            self.imagem.carregar(caminho)
            self.palco.setCurrentWidget(self.imagem)
        elif ext in VIDEO_EXT or ext in AUDIO_EXT:
            gif = bool(
                ((msg.get("message") or {}).get("videoMessage") or {}).get("gifPlayback")
            )
            self.palco.setCurrentWidget(self.player)
            self.player.abrir(caminho, com_video=ext in VIDEO_EXT, loop=gif)
        else:
            self.imagem.carregar("")
            self.palco.setCurrentWidget(self.imagem)

        quem = "Você" if (msg.get("key") or {}).get("fromMe") else (msg.get("pushName") or "")
        quando = datetime.fromtimestamp(msg.get("messageTimestamp", 0)).strftime("%d/%m/%Y %H:%M")
        self.cabecalho.setText(f"{quem}  ·  {quando}   ({self.i + 1}/{len(self.midias)})")
        texto = api.text_of(msg)
        self.legenda.setText("" if texto.startswith(("🖼", "🎬", "🎤", "📄")) else texto)
        self.anterior.setEnabled(self.i > 0)
        self.proximo.setEnabled(self.i < len(self.midias) - 1)
        self._selo_favorita()
        for b in (self.favoritar, self.encaminhar, self.mais):
            b.setVisible(bool(self.menu_de))

    def ir(self, passo: int):
        novo = self.i + passo
        if 0 <= novo < len(self.midias):
            self.i = novo
            self.mostrar()

    def _reagir(self, emoji: str):
        self.reagiu.emit(self._atual(), emoji)

    def _menu(self, global_pos):
        """O mesmo menu da bolha, aqui dentro — botao ⋮ e clique direito."""
        if not self.menu_de:
            return
        self.menu_de(self._atual(), fechar=self.accept).exec(global_pos)
        self._selo_favorita()

    def _selo_favorita(self):
        """A estrela segue o estado da mensagem.

        Favoritar por aqui muta msg["_favorita"], mas nada repintava o botao:
        ele ficava "☆" ate' navegar para outra midia e voltar.
        """
        self.favoritar.setText("★" if self._atual().get("_favorita") else "☆")

    def contextMenuEvent(self, e):
        self._menu(e.globalPos())

    def _disparar(self, *prefixos: str):
        """Botao da barra dispara a acao do proprio menu.

        Assim ⋮, clique direito e os botoes diretos executam o MESMO codigo —
        um so' lugar para consertar, e nenhum deles pode ficar para tras.
        """
        if not self.menu_de:
            return
        menu = self.menu_de(self._atual(), fechar=self.accept)
        for a in menu.actions():
            if a.text().startswith(prefixos):
                a.trigger()
                self._selo_favorita()
                return

    def salvar(self):
        origem = pathlib.Path(self._caminho())
        if not origem.exists():
            return
        destino, _ = QFileDialog.getSaveFileName(self, "Salvar como", origem.name)
        if destino:
            shutil.copy2(origem, destino)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape,):
            self.accept()
        elif e.key() in (Qt.Key_Left,):
            self.ir(-1)
        elif e.key() in (Qt.Key_Right,):
            self.ir(1)
        elif e.key() == Qt.Key_Space:
            self.player.alternar()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.player.parar()
        super().closeEvent(e)

    def accept(self):
        self.player.parar()
        super().accept()


# --------------------------------------------------------------------------- envio


CORES_CANETA = ["#ff3b30", "#34c759", "#0a84ff", "#ffd60a", "#ffffff", "#000000"]
LADO_HD = 1600  # sem HD, o WhatsApp reduz e recomprime; este e' o teto usado aqui


class TelaImagem(QLabel):
    """Preview com anotação à mão livre. Traços em coordenadas normalizadas,
    para sobreviver a redimensionamento e sair certo na resolução original."""

    def __init__(self, caminho: str):
        super().__init__()
        self.original = QPixmap(caminho)
        self.tracos: list[tuple[str, list[tuple[float, float]]]] = []
        self.cor = CORES_CANETA[0]
        self.setMinimumSize(360, 300)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.CrossCursor)

    def _area(self) -> QRect:
        """Onde a imagem realmente aparece dentro do widget."""
        if self.original.isNull():
            return self.rect()
        escala = min(self.width() / self.original.width(), self.height() / self.original.height(), 1.0)
        w, h = int(self.original.width() * escala), int(self.original.height() * escala)
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def _normalizar(self, ponto) -> tuple[float, float] | None:
        a = self._area()
        if not a.contains(ponto) or not a.width() or not a.height():
            return None
        return ((ponto.x() - a.left()) / a.width(), (ponto.y() - a.top()) / a.height())

    def mousePressEvent(self, e):
        if (n := self._normalizar(e.position().toPoint())) is not None:
            self.tracos.append((self.cor, [n]))
            self.update()

    def mouseMoveEvent(self, e):
        if self.tracos and (e.buttons() & Qt.LeftButton):
            if (n := self._normalizar(e.position().toPoint())) is not None:
                self.tracos[-1][1].append(n)
                self.update()

    def desfazer(self):
        if self.tracos:
            self.tracos.pop()
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if self.original.isNull():
            p.setPen(QColor(P["fraco"]))
            p.drawText(self.rect(), Qt.AlignCenter, "sem pré-visualização")
            return
        a = self._area()
        p.drawPixmap(a, self.original)
        self._pintar_tracos(p, a, max(2.0, a.width() / 220))

    def _pintar_tracos(self, p, area, espessura):
        for cor, pontos in self.tracos:
            if len(pontos) < 2:
                continue
            caneta = QPen(QColor(cor), espessura, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(caneta)
            caminho = QPainterPath()
            caminho.moveTo(area.left() + pontos[0][0] * area.width(),
                           area.top() + pontos[0][1] * area.height())
            for x, y in pontos[1:]:
                caminho.lineTo(area.left() + x * area.width(), area.top() + y * area.height())
            p.drawPath(caminho)

    def render_final(self) -> QPixmap:
        """A imagem original com os traços aplicados em escala cheia."""
        if self.original.isNull() or not self.tracos:
            return self.original
        saida = QPixmap(self.original)
        p = QPainter(saida)
        p.setRenderHint(QPainter.Antialiasing)
        self._pintar_tracos(p, QRect(0, 0, saida.width(), saida.height()),
                            max(3.0, saida.width() / 220))
        p.end()
        return saida


class DialogoEnvio(QDialog):
    """Tela de confirmação antes de enviar — como no WhatsApp Web.

    Imagem: pré-visualização, anotação colorida, legenda e escolha de HD.
    Documento: sem preview (o WhatsApp Web também não mostra), só nome e legenda.
    """

    def __init__(self, caminhos: list[pathlib.Path], destino: str, pai=None):
        super().__init__(pai)
        self.caminhos = caminhos
        self.atual = 0
        self.telas: dict[int, TelaImagem] = {}
        self.setWindowTitle(f"Enviar para {destino}")
        self.setMinimumSize(620, 560)

        self.palco = QStackedWidget()
        for i, c in enumerate(caminhos):
            if self._e_imagem(c):
                tela = TelaImagem(str(c))
                self.telas[i] = tela
                self.palco.addWidget(tela)
            else:
                self.palco.addWidget(self._cartao_documento(c))

        self.barra = QHBoxLayout()
        self.botoes_cor = []
        for cor in CORES_CANETA:
            b = QPushButton()
            b.setFixedSize(24, 24)
            b.setStyleSheet(
                f"background:{cor}; border-radius:12px; border:2px solid {P['divisor']};"
            )
            b.clicked.connect(lambda _=False, c=cor: self._escolher_cor(c))
            self.botoes_cor.append((b, cor))
            self.barra.addWidget(b)
        desfazer = QPushButton("↶")
        desfazer.setToolTip("Desfazer traço")
        desfazer.clicked.connect(lambda: self._tela_atual() and self._tela_atual().desfazer())
        self.barra.addWidget(desfazer)
        self.barra.addStretch(1)
        self.hd = QCheckBox("Enviar em HD")
        self.hd.setToolTip(f"Sem HD, a imagem é reduzida para {LADO_HD}px e recomprimida")
        self.barra.addWidget(self.hd)

        self.tiras = QListWidget()
        self.tiras.setFlow(QListWidget.LeftToRight)
        self.tiras.setFixedHeight(74)
        self.tiras.setIconSize(QSize(56, 56))
        for c in caminhos:
            item = QListWidgetItem(QIcon(str(c)) if self._e_imagem(c) else QIcon(), c.name[:14])
            self.tiras.addItem(item)
        self.tiras.setCurrentRow(0)
        self.tiras.currentRowChanged.connect(self._trocar)
        self.tiras.setVisible(len(caminhos) > 1)

        self.legenda = QLineEdit()
        self.legenda.setPlaceholderText("Adicione uma legenda…")
        self.legenda.returnPressed.connect(self.accept)

        acoes = QDialogButtonBox(QDialogButtonBox.Cancel)
        enviar = acoes.addButton("Enviar", QDialogButtonBox.AcceptRole)
        enviar.setDefault(True)
        acoes.accepted.connect(self.accept)
        acoes.rejected.connect(self.reject)

        caixa = QVBoxLayout(self)
        caixa.addWidget(self.palco, 1)
        caixa.addLayout(self.barra)
        caixa.addWidget(self.tiras)
        caixa.addWidget(self.legenda)
        caixa.addWidget(acoes)
        self._trocar(0)
        self.legenda.setFocus()

    @staticmethod
    def _e_imagem(c: pathlib.Path) -> bool:
        return c.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

    def _cartao_documento(self, c: pathlib.Path) -> QWidget:
        tamanho = c.stat().st_size if c.exists() else 0
        unidade = f"{tamanho/1_048_576:.1f} MB" if tamanho > 1_048_576 else f"{tamanho/1024:.0f} KB"
        rotulo = QLabel(f"📄\n\n{c.name}\n{unidade}")
        rotulo.setAlignment(Qt.AlignCenter)
        rotulo.setStyleSheet(f"font-size:16px; color:{P['texto']};")
        return rotulo

    def _tela_atual(self) -> TelaImagem | None:
        return self.telas.get(self.atual)

    def _escolher_cor(self, cor: str):
        if tela := self._tela_atual():
            tela.cor = cor
        for b, c in self.botoes_cor:
            marca = P["texto"] if c == cor else P["divisor"]
            b.setStyleSheet(f"background:{c}; border-radius:12px; border:2px solid {marca};")

    def _tracar_visivel(self, ligado: bool):
        for i in range(self.barra.count()):
            if w := self.barra.itemAt(i).widget():
                if w is not self.hd:
                    w.setVisible(ligado)

    def _trocar(self, linha: int):
        if linha < 0:
            return
        self.atual = linha
        self.palco.setCurrentIndex(linha)
        e_img = self._tela_atual() is not None
        self._tracar_visivel(e_img)
        self.hd.setVisible(e_img)
        if e_img:
            self._escolher_cor(self._tela_atual().cor)

    def preparar(self) -> tuple[list[pathlib.Path], str]:
        """Arquivos finais (com traços e compressão aplicados) e a legenda."""
        saida = []
        for i, c in enumerate(self.caminhos):
            tela = self.telas.get(i)
            if tela is None:
                saida.append(c)
                continue
            imagem = tela.render_final()
            if not self.hd.isChecked() and max(imagem.width(), imagem.height()) > LADO_HD:
                imagem = imagem.scaled(LADO_HD, LADO_HD, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if imagem is tela.original and not tela.tracos and self.hd.isChecked():
                saida.append(c)  # nada mudou: manda o arquivo original
                continue
            destino = api.MEDIA / f"envio-{datetime.now():%Y%m%d-%H%M%S}-{i}.jpg"
            imagem.save(str(destino), "JPEG", 95 if self.hd.isChecked() else 80)
            saida.append(destino)
        return saida, self.legenda.text().strip()


# --------------------------------------------------------------------------- contato


class SeloClicavel(QLabel):
    """Foto de perfil que abre em tamanho grande, como no Web: clicar no
    retrato dos Dados do contato amplia a foto baixada."""

    clicado = Signal()

    def __init__(self, pix: QPixmap, dica: str = ""):
        super().__init__()
        self.setPixmap(pix)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(dica)
        self.setStyleSheet("background:transparent; border:none;")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicado.emit()
        super().mouseReleaseEvent(e)


def seguro(alvo: QObject, fn):
    """`fn` embrulhada para só rodar se `alvo` ainda existir no lado C++.

    Diálogos como DadosDoContato/PainelMidias/MeuPerfil/Privacidade carregam
    dados em POOL e voltam pela GUI via `_na_gui` (direto ou via `Janela.
    _depois`). Com WA_DeleteOnClose, se o usuário fechar o diálogo ANTES da
    consulta terminar, o objeto Python continua existindo (a própria lambda
    o segura vivo) mas o QObject por baixo já foi destruído — chamar um
    método nele levanta `RuntimeError: Internal C++ object already deleted`,
    não falha em silêncio. `alvo` é o diálogo dono da callback, nunca a
    Janela (essa vive a sessão inteira, não precisa deste embrulho).
    """
    return lambda: shiboken6.isValid(alvo) and fn()


def emitir_se_vivo(alvo: QObject, sinal: Signal, fn):
    """`sinal.emit(fn)` protegido por `seguro` — para quem chama `_na_gui`
    diretamente, em vez de passar por `Janela._depois`."""
    sinal.emit(seguro(alvo, fn))


def rotulo_mes(ts: int) -> str:
    """Cabeçalho de grupo por mês na grade de mídia — "NESTE MÊS" para o
    corrente, senão o nome do mês (e o ano, se não for o corrente). É como
    o Web agrupa a aba Mídia do painel (medido no WhatsApp Web)."""
    d = datetime.fromtimestamp(ts)
    agora = datetime.now()
    if (d.year, d.month) == (agora.year, agora.month):
        return "NESTE MÊS"
    nomes = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
             "agosto", "setembro", "outubro", "novembro", "dezembro")
    mes = nomes[d.month - 1].upper()
    return mes if d.year == agora.year else f"{mes} DE {d.year}"


class CartaoClicavel(QFrame):
    """Frame que dispara um sinal ao clicar — base dos cartões de doc/link."""

    clicado = Signal()

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicado.emit()
        super().mouseReleaseEvent(e)


class PainelMidias(QDialog):
    """"Mídia, links e docs" do WhatsApp Web: 3 abas (Mídia/Documentos/Links).

    Abre por CIMA dos Dados do contato, que continuam abertos por baixo. Esc
    fecha só este painel — é o "voltar" nativo do QDialog, sem plumbing extra:
    a mesma tecla que fecha um diálogo modal devolve o de baixo.
    """

    LADO = 100

    def __init__(self, janela, jids: list[str], aba_inicial: str = "midia"):
        super().__init__(janela)
        self.j = janela
        self.jids = jids
        self.setWindowTitle("Mídia, links e docs")
        # sem isto, CADA abertura da tira/linha "Mídia, links e docs" deixava
        # pendurada a árvore inteira (até 150 QPixmap na grade) pelo resto da
        # sessão — o parent (janela) nunca morre, então nada derrubava o
        # diálogo sozinho. Achado do pente fino cruzado hermes/deepseek.
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(560, 700)

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(0, 0, 0, 0)
        raiz.setSpacing(0)

        abas = QHBoxLayout()
        abas.setSpacing(0)
        abas.setContentsMargins(0, 8, 0, 0)
        self.botoes: dict[str, QPushButton] = {}
        for chave, rotulo in [("midia", "Mídia"), ("docs", "Documentos"), ("links", "Links")]:
            b = QPushButton(rotulo)
            b.setCheckable(True)
            b.setProperty("abaFiltro", True)  # reusa o estilo das abas Tudo/Grupos/...
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=chave: self._trocar_aba(c))
            self.botoes[chave] = b
            abas.addWidget(b, 1)
        raiz.addLayout(abas)

        self.pilha = QStackedWidget()
        self.paginas = {c: self._pagina_rolavel() for c in ("midia", "docs", "links")}
        for pag in self.paginas.values():
            self.pilha.addWidget(pag)
        raiz.addWidget(self.pilha, 1)

        self._carregadas: set[str] = set()
        self._trocar_aba(aba_inicial)

    def _pagina_rolavel(self) -> QScrollArea:
        rolagem = QScrollArea()
        rolagem.setWidgetResizable(True)
        rolagem.setStyleSheet("border:none; background:transparent;")
        dentro = QWidget()
        layout = QVBoxLayout(dentro)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        rolagem.setWidget(dentro)
        rolagem.conteudo = layout  # ponytail: guardar o layout aqui evita subclasse só pra isto
        return rolagem

    def _trocar_aba(self, chave: str):
        for k, b in self.botoes.items():
            b.setChecked(k == chave)
        self.pilha.setCurrentWidget(self.paginas[chave])
        if chave not in self._carregadas:
            self._carregadas.add(chave)
            self.paginas[chave].conteudo.addWidget(QLabel("carregando…"))
            POOL.submit(self._carregar, chave)

    def _carregar(self, chave: str):
        try:
            if chave == "links":
                itens = self.j.api.links_do_chat(self.jids)
            else:
                tipos = api.Api.TIPOS_MIDIA if chave == "midia" else api.Api.TIPOS_DOC
                _total, itens = self.j.api.midias_do_chat(self.jids, tipos=tipos, limite=150)
        except (LookupError, RuntimeError) as e:
            motivo = str(e)  # `e` fica fora de escopo dentro da lambda no _na_gui
            emitir_se_vivo(self, self.j._na_gui, lambda: self._preencher(chave, [], motivo))
            return
        emitir_se_vivo(self, self.j._na_gui, lambda: self._preencher(chave, itens, ""))

    def _limpar(self, layout):
        while (item := layout.takeAt(0)) is not None:
            if w := item.widget():
                w.deleteLater()
            if sub := item.layout():
                while (i2 := sub.takeAt(0)) is not None:
                    if w2 := i2.widget():
                        w2.deleteLater()

    def _preencher(self, chave: str, itens: list[dict], erro: str):
        layout = self.paginas[chave].conteudo
        self._limpar(layout)
        if erro:
            layout.addWidget(QLabel(erro))
            return
        if not itens:
            vazio = QLabel("nada por aqui ainda")
            vazio.setStyleSheet(f"color:{P['fraco']}; background:transparent;")
            layout.addWidget(vazio)
            return
        if chave == "midia":
            self._preencher_grade(layout, itens)
        elif chave == "docs":
            for m in itens:
                layout.addWidget(self._cartao_doc(m))
            layout.addStretch(1)
        else:
            for m in itens:
                layout.addWidget(self._cartao_link(m))
            layout.addStretch(1)

    # ---------- Mídia: grade de 3 colunas, agrupada por mês ----------

    def _preencher_grade(self, layout, itens: list[dict]):
        mes_atual, linha, col = None, None, 0
        for m in itens:
            mes = rotulo_mes(m.get("messageTimestamp", 0))
            if mes != mes_atual:
                mes_atual = mes
                cab = QLabel(mes)
                cab.setStyleSheet(
                    f"color:{P['fraco']}; font-size:11px; font-weight:600; "
                    "background:transparent; margin-top:6px;"
                )
                layout.addWidget(cab)
                linha, col = None, 0
            if linha is None or col == 3:
                linha = QHBoxLayout()
                linha.setSpacing(4)
                layout.addLayout(linha)
                col = 0
            linha.addWidget(self._miniatura_grade(m))
            col += 1
        if linha is not None and 0 < col < 3:
            linha.addStretch(3 - col)
        layout.addStretch(1)

    def _miniatura_grade(self, m: dict) -> QLabel:
        lado = self.LADO
        det = api.detalhes(m)
        pix = thumb_embutida(det["thumb"], lado * 2) if det.get("thumb") else QPixmap()
        if pix.isNull():
            pix = QPixmap(lado, lado)
            pix.fill(QColor(P["selecao"]))
        else:
            # cover: corta o excesso mantendo o centro, como a grade do Web
            esc = max(lado / pix.width(), lado / pix.height())
            pix = pix.scaled(max(1, round(pix.width() * esc)), max(1, round(pix.height() * esc)),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x, y = max(0, (pix.width() - lado) // 2), max(0, (pix.height() - lado) // 2)
            pix = pix.copy(x, y, lado, lado)
        if det.get("gif"):
            pix = self._com_selo_gif(pix)
        lbl = SeloClicavel(pix)
        lbl.setFixedSize(lado, lado)
        lbl.clicado.connect(lambda msg=m: self._abrir_midia(msg))
        return lbl

    @staticmethod
    def _com_selo_gif(pix: QPixmap) -> QPixmap:
        """"GIF" no canto — o mesmo selo da grade do seletor de GIF."""
        alvo = QPixmap(pix)
        p = QPainter(alvo)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        selo = QRect(4, alvo.height() - 20, 32, 15)
        p.drawRoundedRect(selo, 3, 3)
        p.setPen(QColor("#ffffff"))
        f = QFont()
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.drawText(selo, Qt.AlignCenter, "GIF")
        p.end()
        return alvo

    def _abrir_midia(self, m: dict):
        """Abre no MESMO visualizador embutido da conversa (menu, ★, ➦...) —
        galeria de 1 item, porque a mensagem pode não estar na janela viva."""
        def trabalho():
            if not (caminho := self.j.api.media_file(m)):
                self.j._na_gui.emit(
                    lambda: self.j.statusBar().showMessage("mídia indisponível")
                )
                return
            m["_arquivo"] = str(caminho)
            emitir_se_vivo(self, self.j._na_gui, lambda: self._abrir_visor(m))

        POOL.submit(trabalho)

    def _abrir_visor(self, m: dict):
        visor = Visualizador([m], 0, self.j.titulo.text(), self,
                             menu_de=self.j.menu_para_mensagem)
        visor.exec()

    # ---------- Documentos: cartão como o do balão, com download ----------

    def _cartao_doc(self, m: dict) -> QFrame:
        det = api.detalhes(m)
        nome = det.get("nome", "documento")
        ext = nome.rsplit(".", 1)[-1][:4].upper() if "." in nome else "DOC"
        c = CartaoClicavel()
        c.setStyleSheet(f"background:{P['selecao']}; border-radius:8px;")
        linha = QHBoxLayout(c)
        linha.setContentsMargins(10, 8, 10, 8)
        icone = QLabel(ext)
        icone.setFixedSize(34, 40)
        icone.setAlignment(Qt.AlignCenter)
        icone.setStyleSheet(
            f"background:{cor_doc(nome)}; color:#ffffff; "
            "border-radius:4px; font-size:10px; font-weight:600;"
        )
        linha.addWidget(icone)
        info = QVBoxLayout()
        titulo = QLabel(nome)
        titulo.setWordWrap(True)
        titulo.setStyleSheet("background:transparent; font-size:13px;")
        detalhe = " • ".join(x for x in (
            f"{det['paginas']} páginas" if det.get("paginas") else "", ext,
            api.tamanho_humano(det.get("bytes", 0)),
        ) if x)
        sub = QLabel(detalhe)
        sub.setStyleSheet(f"color:{P['fraco']}; font-size:11px; background:transparent;")
        info.addWidget(titulo)
        info.addWidget(sub)
        linha.addLayout(info, 1)
        baixar = QLabel("⤓")
        baixar.setStyleSheet(f"color:{P['fraco']}; font-size:16px; background:transparent;")
        linha.addWidget(baixar)
        c.clicado.connect(lambda msg=m: self._abrir_doc(msg))
        return c

    def _abrir_doc(self, m: dict):
        def trabalho():
            if caminho := self.j.api.media_file(m):
                self.j._na_gui.emit(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(caminho)))
                )
            else:
                self.j._na_gui.emit(
                    lambda: self.j.statusBar().showMessage("documento indisponível")
                )

        POOL.submit(trabalho)

    # ---------- Links: mesmo cartão do balão, some' clicável ----------

    def _cartao_link(self, m: dict) -> QFrame:
        det = api.detalhes(m)
        link = det.get("link") or {}
        c = CartaoClicavel()
        c.setStyleSheet(f"background:{P['selecao']}; border-radius:8px;")
        col = QVBoxLayout(c)
        col.setContentsMargins(10, 8, 10, 8)
        col.setSpacing(2)
        if link.get("thumb") and not (pix := link_pix(link["thumb"], 300)).isNull():
            img = QLabel()
            img.setPixmap(pix)
            img.setAlignment(Qt.AlignCenter)
            col.addWidget(img)
        dom = QLabel(dominio_de(link.get("url", "")))
        dom.setStyleSheet(f"color:{P['badge_texto']}; font-size:11px; background:transparent;")
        titulo = QLabel(link.get("titulo") or link.get("url") or "link")
        titulo.setWordWrap(True)
        titulo.setStyleSheet("font-weight:600; background:transparent;")
        col.addWidget(dom)
        col.addWidget(titulo)
        if desc := link.get("descricao"):
            r = QLabel(desc[:120])
            r.setWordWrap(True)
            r.setStyleSheet(f"color:{P['fraco']}; font-size:12px; background:transparent;")
            col.addWidget(r)
        url = link.get("url", "")
        c.clicado.connect(lambda u=url: u and QDesktopServices.openUrl(QUrl(u)))
        return c


class DadosDoContato(QDialog):
    """Painel "Dados do contato" do WhatsApp Web: retrato, recado, mídia,
    silenciar, grupos em comum e as ações da conversa."""

    def __init__(self, janela, chat: dict):
        super().__init__(janela)
        self.j = janela
        self.chat = chat
        self.jids = chat.get("_jids") or [chat.get("remoteJid", "")]
        self.jid = chat.get("remoteJid", "")
        self.e_grupo = self.jid.endswith("@g.us")
        self.setWindowTitle("Dados do contato")
        # mesmo achado do PainelMidias: sem isto o diálogo nunca morre — só
        # esconde — e cada abertura empilha mais um na memória da sessão.
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(560, 760)
        self.resize(600, 860)

        corpo = QVBoxLayout()
        corpo.setSpacing(14)
        corpo.setContentsMargins(20, 20, 20, 20)

        nome = api.nome_do_chat(chat, janela.chats.contatos)
        retrato = foto(chat["_foto"], 132) if chat.get("_foto") else QPixmap()
        selo = SeloClicavel(
            retrato if not retrato.isNull() else avatar(nome, self.jid, 132),
            "Ver foto grande",
        )
        selo.clicado.connect(self._foto_grande)
        corpo.addWidget(selo)

        self.titulo = QLabel(nome)
        self.titulo.setAlignment(Qt.AlignCenter)
        self.titulo.setStyleSheet("font-size:20px; font-weight:600; background:transparent;")
        corpo.addWidget(self.titulo)

        sub = QLabel("grupo" if self.e_grupo else api.formatar_numero(self.jids[-1]))
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(f"color:{P['fraco']}; background:transparent;")
        corpo.addWidget(sub)

        acoes = QHBoxLayout()
        acoes.addStretch(1)
        for icone, rotulo, fn in [
            ("📞", "Ligar", self.ligar), ("🎥", "Vídeo", self.ligar),
            ("🔍", "Pesquisar", self.pesquisar),
        ]:
            b = QPushButton(f"{icone}\n{rotulo}")
            b.setFixedSize(74, 58)
            b.setStyleSheet(
                f"background:{P['selecao']}; border-radius:10px; font-size:12px;"
            )
            b.clicked.connect(fn)
            acoes.addWidget(b)
        acoes.addStretch(1)
        corpo.addLayout(acoes)

        self.recado = QLabel("")
        self.recado.setWordWrap(True)
        self.recado.setVisible(False)
        corpo.addWidget(self._secao("Recado"))
        corpo.addWidget(self.recado)

        # Sem numero: o Web mostra so' o chevron aqui, o total fica no titulo
        # das abas do painel completo.
        self.linha_midia = QPushButton("🖼  Mídia, links e docs                                    ›")
        self.linha_midia.setCursor(Qt.PointingHandCursor)
        self.linha_midia.setStyleSheet(
            "text-align:left; padding:8px; font-size:14px; background:transparent;"
        )
        self.linha_midia.clicked.connect(lambda: self._abrir_painel_midias("midia"))
        corpo.addWidget(self._separador())
        corpo.addWidget(self.linha_midia)
        self.tira = QListWidget()
        self.tira.setFlow(QListWidget.LeftToRight)
        self.tira.setFixedHeight(96)
        self.tira.setIconSize(QSize(80, 80))
        self.tira.setStyleSheet("background:transparent; border:none;")
        self.tira.itemClicked.connect(self._abrir_midia)
        corpo.addWidget(self.tira)

        corpo.addWidget(self._separador())
        b_fav = QPushButton("★   Mensagens favoritas")
        b_fav.setStyleSheet("text-align:left; padding:8px; font-size:14px; background:transparent;")
        b_fav.clicked.connect(lambda: (self.accept(), janela.abrir_favoritas()))
        corpo.addWidget(b_fav)

        linha_mudo = QHBoxLayout()
        rot = QLabel("🔕  Silenciar notificações")
        rot.setStyleSheet("background:transparent;")
        linha_mudo.addWidget(rot, 1)
        self.b_mudo = QPushButton()
        self.b_mudo.setFixedWidth(120)
        self.b_mudo.clicked.connect(self.alternar_mudo)
        menu = QMenu(self.b_mudo)
        for rotulo, segundos in api.DURACOES_MUDO:
            menu.addAction(rotulo).triggered.connect(
                lambda _=False, s=segundos: self.silenciar(s)
            )
        self.b_mudo.setMenu(menu)
        linha_mudo.addWidget(self.b_mudo)
        corpo.addLayout(linha_mudo)
        self._pintar_mudo()

        # Administracao do grupo: so' nasce se for grupo, e as acoes de admin
        # so' aparecem se eu for admin — a API recusa as outras com not-authorized,
        # e botao que so' da erro e' pior que botao ausente.
        self.caixa_admin = QVBoxLayout()
        self.caixa_admin.setContentsMargins(0, 0, 0, 0)
        corpo.addLayout(self.caixa_admin)

        corpo.addWidget(self._separador())
        self.titulo_grupos = QLabel("Grupos em comum")
        self.titulo_grupos.setStyleSheet("background:transparent;")
        cabeca_grupos = QHBoxLayout()
        cabeca_grupos.addWidget(self.titulo_grupos, 1)
        self.b_add = QPushButton("+  Adicionar")
        self.b_add.setVisible(False)
        self.b_add.setStyleSheet("padding:4px 10px; background:transparent;")
        self.b_add.clicked.connect(self.adicionar_participante)
        cabeca_grupos.addWidget(self.b_add)
        corpo.addLayout(cabeca_grupos)
        self.grupos = QListWidget()
        self.grupos.setMinimumHeight(220)
        self.grupos.setWordWrap(True)
        self.grupos.setStyleSheet("background:transparent; border:none;")
        # clique direito no participante, como no Web
        self.grupos.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grupos.customContextMenuRequested.connect(self.menu_do_participante)
        corpo.addWidget(self.grupos)

        corpo.addWidget(self._separador())
        for icone, rotulo, fn in [
            ("🗄", "Arquivar conversa", self.arquivar),
            ("⤓", "Exportar conversa", self.exportar),
            ("⊘", "Bloquear contato", self.bloquear),
        ]:
            b = QPushButton(f"{icone}   {rotulo}")
            b.setStyleSheet(
                "text-align:left; padding:8px; font-size:14px; background:transparent;"
            )
            b.clicked.connect(fn)
            corpo.addWidget(b)

        dentro = QWidget()
        dentro.setLayout(corpo)
        rolagem = QScrollArea()
        rolagem.setWidgetResizable(True)
        rolagem.setWidget(dentro)
        rolagem.setStyleSheet("border:none;")
        fora = QVBoxLayout(self)
        fora.setContentsMargins(0, 0, 0, 0)
        fora.addWidget(rolagem)

        POOL.submit(self._carregar)

    # ---------- montagem ----------

    def _secao(self, texto: str) -> QLabel:
        r = QLabel(texto)
        r.setStyleSheet(f"color:{P['fraco']}; font-size:12px; background:transparent;")
        return r

    def _separador(self) -> QFrame:
        linha = QFrame()
        linha.setFrameShape(QFrame.HLine)
        linha.setStyleSheet(f"color:{P['divisor']};")
        return linha

    def _carregar(self):
        """Roda no POOL; cada callback de volta pela GUI passa por
        `emitir_se_vivo` porque o usuário pode fechar este diálogo (WA_
        DeleteOnClose) antes de alguma dessas consultas terminar."""
        if not self.e_grupo:
            perfil = self.j.api.perfil(self.jids[-1])
            if recado := perfil.get("recado"):
                emitir_se_vivo(self, self.j._na_gui, lambda: (
                    self.recado.setText(recado), self.recado.setVisible(True)
                ))
        try:
            total, itens = self.j.api.midias_do_chat(self.jids)
        except LookupError:
            emitir_se_vivo(self, self.j._na_gui, lambda: self.linha_midia.setText(
                "Mídia, links e docs — configure o Postgres no ⚙"
            ))
            total, itens = None, []
        except RuntimeError as e:
            recado = str(e)
            emitir_se_vivo(self, self.j._na_gui,
                          lambda: self.linha_midia.setText(f"Mídia, links e docs — {recado}"))
            total, itens = None, []
        if total is not None:
            emitir_se_vivo(self, self.j._na_gui, lambda: self._pintar_midias(total, itens))

        if self.e_grupo:
            info = self.j.api.grupo_info(self.jid)
            partes = info.get("participants") or []
            # o link so' vem para admin; para o resto a API devolve not-authorized
            link = self.j.api.grupo_convite(self.jid) if self.j.api.sou_admin(info) else ""
            emitir_se_vivo(self, self.j._na_gui, lambda: (
                self._pintar_participantes(partes), self._pintar_admin(info, link)
            ))
        else:
            comuns = self.j.api.grupos_em_comum(self.jids)
            emitir_se_vivo(self, self.j._na_gui, lambda: self._pintar_grupos(comuns))

    def _pintar_midias(self, _total: int, itens: list):
        """Tira de 4, como o Web — a MESMA thumb embutida que a bolha usa, sem
        baixar arquivo nenhum (o `media_file` daqui rodava na GUI e travava a
        janela por cada uma das 12 mídias antigas)."""
        for m in itens[:4]:
            det = api.detalhes(m)
            lado = 80
            pix = thumb_embutida(det["thumb"], lado * 2) if det.get("thumb") else QPixmap()
            if pix.isNull():
                nome = det.get("nome", "arquivo")
                ext = nome.rsplit(".", 1)[-1][:4].upper() if "." in nome else "DOC"
                pix = QPixmap(lado, lado)
                pix.fill(QColor(cor_doc(nome)))
                p = QPainter(pix)
                p.setPen(QColor("#ffffff"))
                f = QFont()
                f.setPixelSize(14)
                f.setBold(True)
                p.setFont(f)
                p.drawText(pix.rect(), Qt.AlignCenter, ext)
                p.end()
            else:
                esc = max(lado / pix.width(), lado / pix.height())
                pix = pix.scaled(max(1, round(pix.width() * esc)), max(1, round(pix.height() * esc)),
                                 Qt.KeepAspectRatio, Qt.SmoothTransformation)
                x, y = max(0, (pix.width() - lado) // 2), max(0, (pix.height() - lado) // 2)
                pix = pix.copy(x, y, lado, lado)
                if det.get("gif"):
                    pix = PainelMidias._com_selo_gif(pix)
            item = QListWidgetItem(QIcon(pix), "")
            item.setToolTip(api.text_of(m)[:60])
            self.tira.addItem(item)

    def _pintar_grupos(self, comuns: list):
        self.titulo_grupos.setText(f"{len(comuns)} grupos em comum")
        for g in comuns:
            nomes = ", ".join(
                api.formatar_numero(str(p.get("phoneNumber") or p.get("id", "")))
                for p in (g.get("participants") or [])[:4]
            )
            item = QListWidgetItem(f"{g.get('subject', '—')}\n{nomes}")
            item.setData(Qt.UserRole, g.get("id"))
            self.grupos.addItem(item)
        self.grupos.itemClicked.connect(self._abrir_grupo)

    def _pintar_participantes(self, partes: list):
        self.titulo_grupos.setText(f"{len(partes)} participantes")
        self.grupos.clear()
        for x in partes:
            marca = "  (admin)" if x.get("admin") else ""
            numero = str(x.get("phoneNumber") or x.get("id", ""))
            nome = self.j.chats.contatos.get(numero) or api.formatar_numero(numero)
            item = QListWidgetItem(nome + marca)
            # o numero cru fica no item: e' dele que as acoes de admin precisam
            item.setData(Qt.UserRole, {"numero": numero, "admin": bool(x.get("admin"))})
            self.grupos.addItem(item)

    def _pintar_admin(self, info: dict, link: str):
        """Seção de administração — só o que a API deixa eu fazer de fato.

        Repintar é o caso normal, não a exceção: toda ação chama _apos(), que
        recarrega. Sem esvaziar antes, trocar uma permissão empilhava a seção
        inteira de novo — dois "Sair do grupo", três, quatro.
        """
        while (item := self.caixa_admin.takeAt(0)) is not None:
            if w := item.widget():
                w.deleteLater()
        self.info_grupo = info
        self.link_convite = link
        sou_admin = self.j.api.sou_admin(info)
        self.b_add.setVisible(sou_admin)

        def botao(rotulo, fn, menu=None):
            b = QPushButton(rotulo)
            b.setStyleSheet(
                "text-align:left; padding:8px; font-size:14px; background:transparent;"
            )
            if menu is not None:
                # o menu vira filho do BOTAO: setMenu nao toma posse, e com o
                # dialogo como pai cada repintura deixava 3 QMenu pendurados
                # (medido: 3 -> 66 em 21 repinturas)
                menu.setParent(b)
                b.setMenu(menu)
            else:
                b.clicked.connect(fn)
            self.caixa_admin.addWidget(b)
            return b

        self.caixa_admin.addWidget(self._separador())
        if desc := (info.get("desc") or "").strip():
            rot = QLabel(desc)
            rot.setWordWrap(True)
            rot.setStyleSheet(f"color:{P['fraco']}; background:transparent; padding:2px 8px;")
            self.caixa_admin.addWidget(rot)

        if sou_admin:
            botao("✎   Editar nome do grupo", self.editar_assunto)
            botao("✎   Editar descrição", self.editar_descricao)

            trava = QMenu(self)
            envia = info.get("announce")
            trava.addAction("Todos podem enviar" + (" ✓" if not envia else "")).triggered.connect(
                lambda: self._ajuste("not_announcement")
            )
            trava.addAction("Somente admins enviam" + (" ✓" if envia else "")).triggered.connect(
                lambda: self._ajuste("announcement")
            )
            trava.addSeparator()
            edita = info.get("restrict")
            trava.addAction("Todos editam os dados" + (" ✓" if not edita else "")).triggered.connect(
                lambda: self._ajuste("unlocked")
            )
            trava.addAction("Somente admins editam" + (" ✓" if edita else "")).triggered.connect(
                lambda: self._ajuste("locked")
            )
            botao("⚙   Permissões do grupo", None, menu=trava)

            temp = QMenu(self)
            for rotulo, seg in [("Desligadas", 0), ("24 horas", 86400),
                                ("7 dias", 7 * 86400), ("90 dias", 90 * 86400)]:
                temp.addAction(rotulo).triggered.connect(
                    lambda _=False, x=seg: self._temporarias(x)
                )
            botao("⏳   Mensagens temporárias", None, menu=temp)

        if link:
            convite = QMenu(self)
            convite.addAction("⧉  Copiar link").triggered.connect(
                lambda: (QApplication.clipboard().setText(self.link_convite),
                         self.j.statusBar().showMessage("link de convite copiado"))
            )
            convite.addAction("↻  Gerar link novo (invalida o atual)").triggered.connect(
                self.revogar_convite
            )
            botao("🔗   Link de convite", None, menu=convite)

        botao("🚪   Sair do grupo", self.sair_do_grupo)

    # ---------- ações de grupo ----------

    def _apos(self, recado: str):
        """Recarrega o painel: quem muda permissão quer ver o estado novo."""
        self.j.statusBar().showMessage(recado)
        POOL.submit(self._carregar)

    def editar_assunto(self):
        atual = (self.info_grupo or {}).get("subject", "")
        novo, ok = QInputDialog.getText(self, "Nome do grupo", "Nome:", text=atual)
        if ok and novo.strip() and novo.strip() != atual:
            POOL.submit(self.j._depois, seguro(self, lambda: self._apos("nome do grupo alterado")),
                        self.j.api.grupo_assunto, self.jid, novo.strip())

    def editar_descricao(self):
        atual = (self.info_grupo or {}).get("desc", "") or ""
        novo, ok = QInputDialog.getMultiLineText(self, "Descrição do grupo", "Descrição:", atual)
        if ok and novo.strip() != atual.strip():
            POOL.submit(self.j._depois, seguro(self, lambda: self._apos("descrição alterada")),
                        self.j.api.grupo_descricao, self.jid, novo.strip())

    def _ajuste(self, acao: str):
        POOL.submit(self.j._depois, seguro(self, lambda: self._apos("permissões atualizadas")),
                    self.j.api.grupo_ajuste, self.jid, acao)

    def _temporarias(self, segundos: int):
        rotulo = "desligadas" if not segundos else f"{segundos // 86400} dia(s)"
        POOL.submit(self.j._depois, seguro(self, lambda: self._apos(f"mensagens temporárias: {rotulo}")),
                    self.j.api.grupo_temporarias, self.jid, segundos)

    def revogar_convite(self):
        if QMessageBox.question(
            self, "Gerar link novo",
            "O link atual para de funcionar para todo mundo que o tiver. Continuar?"
        ) != QMessageBox.Yes:
            return
        POOL.submit(self.j._depois, seguro(self, lambda: self._apos("link de convite renovado")),
                    self.j.api.grupo_revogar_convite, self.jid)

    def adicionar_participante(self):
        numero, ok = QInputDialog.getText(
            self, "Adicionar ao grupo", "Número com DDI e DDD (ex.: 5581999998888):"
        )
        if not ok or not numero.strip():
            return
        so_digitos = "".join(c for c in numero if c.isdigit())
        if len(so_digitos) < 10:
            QMessageBox.warning(self, "Número inválido", "Faltam dígitos — use DDI + DDD + número.")
            return
        if QMessageBox.question(
            self, "Adicionar", f"Adicionar +{so_digitos} a “{(self.info_grupo or {}).get('subject', '')}”?"
        ) != QMessageBox.Yes:
            return
        POOL.submit(self.j._depois, seguro(self, lambda: self._apos("participante adicionado")),
                    self.j.api.grupo_participante, self.jid, "add", [so_digitos])

    def menu_do_participante(self, ponto: QPoint):
        item = self.grupos.itemAt(ponto)
        if not item or not self.e_grupo:
            return
        dado = item.data(Qt.UserRole)
        if not isinstance(dado, dict):
            return
        numero = "".join(c for c in dado["numero"] if c.isdigit())
        eu = "".join(c for c in (self.j.api.me or "") if c.isdigit())
        menu = QMenu(self)
        menu.addAction("💬  Conversar").triggered.connect(
            lambda: self._conversar_com(numero)
        )
        if self.j.api.sou_admin(self.info_grupo or {}) and numero != eu:
            menu.addSeparator()
            if dado["admin"]:
                menu.addAction("↓  Remover de admin").triggered.connect(
                    lambda: self._participante("demote", numero, "removido de admin")
                )
            else:
                menu.addAction("↑  Tornar admin").triggered.connect(
                    lambda: self._participante("promote", numero, "promovido a admin")
                )
            menu.addAction("✕  Remover do grupo").triggered.connect(
                lambda: self._remover(numero)
            )
        menu.exec(self.grupos.viewport().mapToGlobal(ponto))

    def _conversar_com(self, numero: str):
        """Abre a conversa 1:1 do participante. O mesmo caminho de _abrir_grupo:
        a lista trabalha por indice, entao o jid precisa virar linha."""
        alvos = {f"{numero}@s.whatsapp.net", f"{numero}@lid"}
        for i, c in enumerate(self.j.chats.itens):
            if set(c.get("_jids") or [c.get("remoteJid")]) & alvos:
                self.accept()
                self.j.lista.setCurrentIndex(self.j.chats.index(i))
                self.j.abrir_chat(self.j.chats.index(i))
                return
        self.j.statusBar().showMessage(f"+{numero} ainda não tem conversa aberta")

    def _participante(self, acao: str, numero: str, recado: str):
        POOL.submit(self.j._depois, seguro(self, lambda: self._apos(recado)),
                    self.j.api.grupo_participante, self.jid, acao, [numero])

    def _remover(self, numero: str):
        if QMessageBox.question(
            self, "Remover do grupo", f"Remover +{numero} do grupo?"
        ) != QMessageBox.Yes:
            return
        self._participante("remove", numero, "participante removido")

    def sair_do_grupo(self):
        nome = (self.info_grupo or {}).get("subject", "este grupo")
        if QMessageBox.question(
            self, "Sair do grupo",
            f"Sair de “{nome}”?\n\nPara voltar você precisa de um convite novo."
        ) != QMessageBox.Yes:
            return
        POOL.submit(self.j._depois, seguro(self, lambda: (self.accept(), self._apos("você saiu do grupo"))),
                    self.j.api.grupo_sair, self.jid)

    # ---------- ações ----------

    def _pintar_mudo(self):
        fim = self.j.mudos.get(self.jid)
        if not api.esta_mudo(self.j.mudos, self.jid):
            self.b_mudo.setText("Ativado")
        elif fim == 0:
            self.b_mudo.setText("🔕 Sempre")
        else:
            self.b_mudo.setText("🔕 " + datetime.fromtimestamp(fim).strftime("até %d/%m %H:%M"))

    def silenciar(self, segundos: int):
        self.j.silenciar(self.jid, segundos)
        self._pintar_mudo()

    def alternar_mudo(self):
        if api.esta_mudo(self.j.mudos, self.jid):
            self.j.silenciar(self.jid, None)
            self._pintar_mudo()

    def ligar(self):
        QMessageBox.information(
            self, "Chamadas",
            "Chamadas de voz e vídeo não funcionam por aqui.\n\n"
            "O protocolo de voz do WhatsApp é fechado: nenhuma biblioteca não "
            "oficial o implementa. O endpoint /call/offer existe, mas só faz o "
            "telefone do outro lado tocar — não transporta áudio.\n\n"
            "Use o celular ou o WhatsApp Web oficial.",
        )

    def pesquisar(self):
        self.accept()
        self.j.abrir_busca_na_conversa()

    def arquivar(self):
        self.j._arquivar_chat(self.chat)
        self.accept()

    def bloquear(self):
        if QMessageBox.question(self, "Bloquear", f"Bloquear {self.titulo.text()}?") == QMessageBox.Yes:
            POOL.submit(self.j._tentar, self.j.api.bloquear, self.jid, True)
            self.accept()

    def exportar(self):
        destino, _ = QFileDialog.getSaveFileName(
            self, "Exportar conversa", f"{self.titulo.text()}.txt", "Texto (*.txt)"
        )
        if not destino:
            return
        linhas = []
        for m in self.j.msgs.itens:
            quem = "Você" if (m.get("key") or {}).get("fromMe") else (m.get("pushName") or "")
            quando = datetime.fromtimestamp(m.get("messageTimestamp", 0)).strftime("%d/%m/%Y %H:%M")
            linhas.append(f"[{quando}] {quem}: {api.text_of(m)}")
        pathlib.Path(destino).write_text("\n".join(linhas), encoding="utf-8")
        self.j.statusBar().showMessage(f"{len(linhas)} mensagens exportadas")

    def _abrir_midia(self, _item):
        """Clicar em qualquer miniatura da tira abre o painel completo — como
        clicar na grade do Web leva para a galeria."""
        self._abrir_painel_midias("midia")

    def _abrir_painel_midias(self, aba: str = "midia"):
        PainelMidias(self.j, self.jids, aba).exec()

    def _foto_grande(self):
        """Clique no retrato: abre a foto baixada no visualizador embutido."""
        caminho = self.chat.get("_foto") or ""
        if not pathlib.Path(caminho).exists():
            return
        msg = {
            "key": {"id": f"perfil-{self.jid}", "remoteJid": self.jid},
            "messageType": "imageMessage", "_arquivo": caminho,
            "message": {"imageMessage": {}},
        }
        nome = api.nome_do_chat(self.chat, self.j.chats.contatos)
        visor = Visualizador([msg], 0, nome, self)
        visor.reagiu.connect(self.j.reagir)
        visor.exec()

    def _abrir_grupo(self, item):
        jid = item.data(Qt.UserRole)
        for i, c in enumerate(self.j.chats.itens):
            if c.get("remoteJid") == jid:
                self.accept()
                self.j.lista.setCurrentIndex(self.j.chats.index(i))
                self.j.abrir_chat(self.j.chats.index(i))
                return


# --------------------------------------------------------------------------- nova conversa


class NovaConversa(QDialog):
    """"Enviar mensagem para número" do WhatsApp Web: conversar com quem não
    está na lista, sem salvar contato antes."""

    LIMITE = 500

    def __init__(self, janela):
        super().__init__(janela)
        self.j = janela
        self.destino = ""
        self.setWindowTitle("Enviar mensagem para número")
        self.setMinimumWidth(ESCALA.e(520))

        titulo = QLabel("Enviar mensagem para número")
        titulo.setStyleSheet(
            f"font-size:{ESCALA.px() + 5}px; font-weight:600; background:transparent;"
        )
        sub = QLabel("Converse com alguém que não está na sua lista de contatos.")
        sub.setStyleSheet(f"color:{P['fraco']}; background:transparent;")

        self.pais = QComboBox()
        self.pais.setEditable(True)
        for iso, nome, ddi in api.PAISES:
            self.pais.addItem(f"{api.bandeira(iso)}  {nome}  +{ddi}", ddi)
        self.pais.setCurrentIndex(0)

        self.numero = QLineEdit()
        self.numero.setPlaceholderText("Exemplo: 11 99999-9999")
        self.numero.textChanged.connect(self._validar)
        self.pais.currentTextChanged.connect(lambda _: self._validar())
        self.numero.returnPressed.connect(self._enviar)

        linha = QHBoxLayout()
        col_pais = QVBoxLayout()
        col_pais.addWidget(self._rot("Código do país"))
        col_pais.addWidget(self.pais)
        col_num = QVBoxLayout()
        col_num.addWidget(self._rot("Número de telefone"))
        col_num.addWidget(self.numero)
        linha.addLayout(col_pais, 2)
        linha.addLayout(col_num, 3)

        self.dica = QLabel("Informe o número nacional, sem o código do país.")
        self.dica.setStyleSheet(f"color:{P['fraco']}; background:transparent;")

        topo_msg = QHBoxLayout()
        topo_msg.addWidget(self._rot("Mensagem opcional"), 1)
        self.contador = QLabel(f"0/{self.LIMITE}")
        self.contador.setStyleSheet(f"color:{P['fraco']}; background:transparent;")
        topo_msg.addWidget(self.contador)

        self.texto = QTextEdit()
        self.texto.setPlaceholderText("Olá! Tudo bem?")
        self.texto.setFixedHeight(ESCALA.e(120))
        self.texto.textChanged.connect(self._contar)

        aviso = QLabel(
            "A mensagem sai da sua instância direto para o número informado.\n"
            "Conversar com desconhecido em volume é o que costuma gerar bloqueio."
        )
        aviso.setWordWrap(True)
        aviso.setStyleSheet(
            f"background:{P['selecao']}; border-radius:8px; padding:10px;"
            f" color:{P['fraco']};"
        )

        self.botoes = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.enviar = self.botoes.addButton("Enviar mensagem", QDialogButtonBox.AcceptRole)
        self.enviar.setEnabled(False)
        self.botoes.accepted.connect(self._enviar)
        self.botoes.rejected.connect(self.reject)

        caixa = QVBoxLayout(self)
        caixa.setSpacing(10)
        caixa.addWidget(titulo)
        caixa.addWidget(sub)
        caixa.addLayout(linha)
        caixa.addWidget(self.dica)
        caixa.addLayout(topo_msg)
        caixa.addWidget(self.texto)
        caixa.addWidget(aviso)
        caixa.addWidget(self.botoes)
        self.numero.setFocus()

    def _rot(self, texto: str) -> QLabel:
        r = QLabel(texto)
        r.setStyleSheet("background:transparent; font-weight:600;")
        return r

    def _ddi(self) -> str:
        """O combo é editável: se o texto foi digitado à mão, `currentData()`
        continua devolvendo o país que estava selecionado antes."""
        texto = self.pais.currentText().strip()
        i = self.pais.findText(texto)
        if i >= 0 and self.pais.itemData(i):
            return self.pais.itemData(i)
        return "".join(c for c in texto if c.isdigit())

    def _contar(self):
        n = len(self.texto.toPlainText())
        if n > self.LIMITE:
            self.texto.setPlainText(self.texto.toPlainText()[: self.LIMITE])
            self.texto.moveCursor(QTextCursor.End)
            n = self.LIMITE
        self.contador.setText(f"{n}/{self.LIMITE}")

    def _validar(self):
        completo = api.montar_numero(self._ddi(), self.numero.text())
        pronto = len(completo) >= 10
        self.enviar.setEnabled(pronto)
        self.dica.setText(
            f"Vai para +{completo}" if pronto
            else "Informe o número nacional, sem o código do país."
        )

    def _enviar(self):
        completo = api.montar_numero(self._ddi(), self.numero.text())
        if len(completo) < 10:
            return
        corpo = self.texto.toPlainText().strip()
        self.enviar.setEnabled(False)
        self.dica.setText("verificando se o número tem WhatsApp…")

        def trabalho():
            achado = self.j.api.existe_no_whatsapp([completo]).get(completo, {})
            self.j._na_gui.emit(lambda: self._resposta(achado, completo, corpo))

        POOL.submit(trabalho)

    def _resposta(self, achado: dict, completo: str, corpo: str):
        if not achado.get("exists"):
            self.dica.setText(f"+{completo} não tem WhatsApp.")
            self.enviar.setEnabled(True)
            return
        self.destino = achado.get("jid") or f"{completo}@s.whatsapp.net"
        if corpo:
            POOL.submit(self.j._tentar, self.j.api.send_text, self.destino, corpo)
        self.accept()


# --------------------------------------------------------------------------- config


class MeuPerfil(QDialog):
    """"Meu perfil" do WhatsApp Web: foto, nome e recado do proprio dono.

    Cada campo grava sozinho, como no Web — nao ha botao "Salvar" global. O
    nome e o recado sao visiveis para os contatos, entao os dois confirmam
    antes de ir; trocar a foto abre o seletor de arquivo, que ja' e' a
    confirmacao.
    """

    def __init__(self, janela):
        super().__init__(janela)
        self.j = janela
        self.setWindowTitle("Meu perfil")
        self.setAttribute(Qt.WA_DeleteOnClose)  # mesmo achado do PainelMidias
        self.setMinimumWidth(ESCALA.e(380))
        self.dados: dict = {}

        self.retrato = QLabel()
        self.retrato.setAlignment(Qt.AlignCenter)
        self.retrato.setFixedHeight(ESCALA.e(150))

        b_foto = QPushButton("Trocar foto")
        b_foto.clicked.connect(self.trocar_foto)
        b_tirar = QPushButton("Remover foto")
        b_tirar.clicked.connect(self.remover_foto)
        linha_foto = QHBoxLayout()
        linha_foto.addStretch(1)
        linha_foto.addWidget(b_foto)
        linha_foto.addWidget(b_tirar)
        linha_foto.addStretch(1)

        self.nome = QLineEdit()
        self.nome.setPlaceholderText("como seus contatos te veem")
        self.nome.setMaxLength(25)          # o WhatsApp corta em 25
        b_nome = QPushButton("Salvar nome")
        b_nome.clicked.connect(self.salvar_nome)

        self.recado = QLineEdit()
        self.recado.setPlaceholderText("recado (status de texto)")
        self.recado.setMaxLength(139)       # limite do WhatsApp
        b_recado = QPushButton("Salvar recado")
        b_recado.clicked.connect(self.salvar_recado)

        form = QFormLayout()
        form.addRow("Nome", self.nome)
        form.addRow("", b_nome)
        form.addRow("Recado", self.recado)
        form.addRow("", b_recado)

        self.aviso = QLabel("")
        self.aviso.setWordWrap(True)
        self.aviso.setStyleSheet(f"color:{P['fraco']}; background:transparent;")

        fechar = QPushButton("Fechar")
        fechar.clicked.connect(self.accept)

        caixa = QVBoxLayout(self)
        caixa.addWidget(self.retrato)
        caixa.addLayout(linha_foto)
        caixa.addLayout(form)
        caixa.addWidget(self.aviso)
        caixa.addWidget(fechar)
        POOL.submit(self._carregar)

    def _carregar(self):
        d = self.j.api.meu_perfil()
        caminho = self.j.api.avatar_file(
            {"remoteJid": f"{self.j.api.me}@s.whatsapp.net", "profilePicUrl": d.get("foto", "")}
        )
        emitir_se_vivo(self, self.j._na_gui, lambda: self._pintar(d, caminho))

    def _pintar(self, d: dict, caminho):
        self.dados = d
        self.nome.setText(d.get("nome", ""))
        self.recado.setText(d.get("recado", ""))
        lado = ESCALA.e(140)
        if caminho:
            self.retrato.setPixmap(foto(str(caminho), lado))
        else:
            self.retrato.setPixmap(avatar(d.get("nome") or "eu", self.j.api.me or "eu", lado))

    def _reverter_campos(self):
        """Volta nome e recado ao que o servidor tem. NAO mexe no retrato: a
        falha pode ser de texto, e trocar a foto por iniciais seria pior."""
        self.nome.setText(self.dados.get("nome", ""))
        self.recado.setText(self.dados.get("recado", ""))

    def _grava(self, fn, valor, recado_ok: str):
        """Grava e, se falhar, DESFAZ o campo na tela.

        _depois so' avisa na barra de status, que fica atras do modal: sem
        isto o campo seguia mostrando o texto novo como se tivesse gravado, e
        so' ao reabrir o dialogo o valor antigo reaparecia.
        """
        def trabalho():
            try:
                fn(valor)
            except Exception as erro:
                # `e` de `except ... as e` some ao sair do bloco: a lambda
                # quebraria com NameError exatamente no caminho de falha.
                log.error("perfil falhou: %s", erro)
                recado = f"não salvou: {erro}"
                emitir_se_vivo(self, self.j._na_gui, lambda: (
                    self.aviso.setText(recado),
                    self._reverter_campos(),
                ))
                return
            emitir_se_vivo(self, self.j._na_gui, lambda: self.aviso.setText(recado_ok))
            POOL.submit(self._carregar)
        POOL.submit(trabalho)

    def salvar_nome(self):
        novo = self.nome.text().strip()
        if not novo or novo == self.dados.get("nome"):
            return
        if QMessageBox.question(
            self, "Nome do perfil",
            f"Seus contatos passam a te ver como “{novo}”. Continuar?"
        ) != QMessageBox.Yes:
            return
        self._grava(self.j.api.perfil_nome, novo, "nome atualizado")

    def salvar_recado(self):
        novo = self.recado.text().strip()
        if novo == (self.dados.get("recado") or ""):
            return
        if QMessageBox.question(
            self, "Recado", "O recado fica visível para os seus contatos. Continuar?"
        ) != QMessageBox.Yes:
            return
        self._grava(self.j.api.perfil_recado, novo, "recado atualizado")

    def trocar_foto(self):
        arq, _ = QFileDialog.getOpenFileName(
            self, "Escolher foto", "", "Imagens (*.jpg *.jpeg *.png *.webp)"
        )
        if not arq:
            return
        self._grava(self.j.api.perfil_foto, pathlib.Path(arq), "foto atualizada")

    def remover_foto(self):
        if QMessageBox.question(
            self, "Remover foto", "Remover sua foto de perfil?"
        ) != QMessageBox.Yes:
            return
        def feito():
            self.aviso.setText("foto removida")
            POOL.submit(self._carregar)
        POOL.submit(self.j._depois, seguro(self, feito), self.j.api.perfil_remover_foto)


class Privacidade(QDialog):
    """Privacidade do WhatsApp Web. Os rotulos e os valores aceitos saem de
    api.Api.PRIVACIDADE, que veio dos tipos do Baileys — nada digitado a mao.

    A rota SUBSTITUI o conjunto: mandar so' a chave mudada zeraria as outras
    cinco. Por isso guardamos tudo e reenviamos o conjunto inteiro.
    """

    ROTULOS = {
        "all": "Todos", "contacts": "Meus contatos",
        "contact_blacklist": "Meus contatos, exceto…", "none": "Ninguém",
        "match_last_seen": "Igual ao visto por último",
    }

    def __init__(self, janela):
        super().__init__(janela)
        self.j = janela
        self.setWindowTitle("Privacidade")
        self.setAttribute(Qt.WA_DeleteOnClose)  # mesmo achado do PainelMidias
        self.setMinimumWidth(ESCALA.e(420))
        self.atual: dict = {}
        self.campos: dict[str, QComboBox] = {}

        form = QFormLayout()
        for chave, (rotulo, aceitos) in self.j.api.PRIVACIDADE.items():
            cb = QComboBox()
            for v in aceitos:
                cb.addItem(self.ROTULOS.get(v, v), v)
            cb.setEnabled(False)          # so' libera quando o estado chegar
            cb.currentIndexChanged.connect(
                lambda _=0, k=chave: self._mudou(k)
            )
            self.campos[chave] = cb
            form.addRow(rotulo, cb)

        self.aviso = QLabel("carregando…")
        self.aviso.setWordWrap(True)
        self.aviso.setStyleSheet(f"color:{P['fraco']}; background:transparent;")
        fechar = QPushButton("Fechar")
        fechar.clicked.connect(self.accept)

        caixa = QVBoxLayout(self)
        caixa.addLayout(form)
        caixa.addWidget(self.aviso)
        caixa.addWidget(fechar)
        POOL.submit(self._carregar)

    def _carregar(self):
        d = self.j.api.privacidade()
        emitir_se_vivo(self, self.j._na_gui, lambda: self._pintar(d))

    def _pintar(self, d: dict):
        self.atual = dict(d)
        for chave, cb in self.campos.items():
            valor = d.get(chave)
            i = cb.findData(valor)
            cb.blockSignals(True)          # pintar nao pode disparar gravacao
            cb.setCurrentIndex(i if i >= 0 else 0)
            cb.blockSignals(False)
            cb.setEnabled(True)
            if i < 0 and valor is not None:
                # valor que a lista do Baileys nao previa: mostrar, nao esconder.
                # Repintar nao duplica: o findData acima ja' acha o item
                # acrescentado antes (medido: 3 itens estaveis em 5 pinturas).
                cb.blockSignals(True)
                cb.addItem(f"{valor} (desconhecido)", valor)
                cb.setCurrentIndex(cb.count() - 1)
                cb.blockSignals(False)
        self.aviso.setText("cada mudança grava na hora, como no WhatsApp Web")

    def _mudou(self, chave: str):
        if not self.atual:
            return
        novo = self.campos[chave].currentData()
        if novo == self.atual.get(chave):
            return
        # As 6 chaves SEMPRE. Filtrar as desconhecidas seria pior que inutil:
        # a rota aplica as seis incondicionalmente e chave ausente chega como
        # `undefined` no Baileys. O valor herdado do servidor vai de volta como
        # veio.
        conjunto = dict(self.atual)
        conjunto[chave] = novo
        if novo not in self.j.api.PRIVACIDADE[chave][1]:
            self.aviso.setText(f"valor {novo!r} não é aceito pela API")
            return

        rotulo = self.j.api.PRIVACIDADE[chave][0]

        # Atualizacao OTIMISTA, antes de submeter. Sem isto, mudar duas chaves
        # em sequencia rapida mandava dois conjuntos concorrentes, cada um com
        # a outra chave no valor VELHO — e como a rota substitui as 6, a
        # segunda escrita desfazia a primeira em silencio.
        anterior = self.atual.get(chave)
        self.atual[chave] = novo

        def feito():
            self.aviso.setText(f"{rotulo}: {self.ROTULOS.get(novo, novo)}")

        def falhou():
            # desfaz a atualizacao otimista: fingir que gravou seria pior
            self.atual[chave] = anterior
            emitir_se_vivo(self, self.j._na_gui, lambda: self._pintar(self.atual))

        # a rota chama reloadConnection() no servidor — a sessao religa
        self.aviso.setText(f"gravando {rotulo}… (a sessão vai religar)")
        POOL.submit(self._gravar, conjunto, feito, falhou)

    def _gravar(self, conjunto: dict, feito, falhou):
        try:
            self.j.api.privacidade_definir(conjunto)
        except Exception as e:
            log.error("privacidade falhou: %s", e)
            self.j._aviso.emit(f"privacidade: {e}")
            falhou()
            return
        emitir_se_vivo(self, self.j._na_gui, feito)


class Config(QDialog):
    """Painel de configuração: conexões e tema. Grava no config.toml."""

    CAMPOS = [
        ("host", "Evolution — URL", "http://localhost:8080"),
        ("apikey", "Evolution — apikey", ""),
        ("instance", "Evolution — instância", "minha-instancia"),
        ("me", "Meu número", ""),
        ("pg_host", "Postgres — host", "localhost"),
        ("pg_port", "Postgres — porta", "5432"),
        ("pg_user", "Postgres — usuário", "evolution"),
        ("pg_password", "Postgres — senha", ""),
        ("pg_database", "Postgres — banco", "evolution"),
        ("giphy_apikey", "Giphy — API key (busca de GIFs)", ""),
    ]

    def __init__(self, cfg: dict, pai=None):
        super().__init__(pai)
        self.setWindowTitle("Configuração")
        self.setMinimumWidth(460)
        self.cfg = dict(cfg)

        form = QFormLayout()
        self.entradas: dict[str, QLineEdit] = {}
        for chave, rotulo, exemplo in self.CAMPOS:
            campo = QLineEdit(str(cfg.get(chave, "")))
            campo.setPlaceholderText(exemplo)
            if "apikey" in chave or "password" in chave:
                campo.setEchoMode(QLineEdit.Password)
            self.entradas[chave] = campo
            form.addRow(rotulo, campo)

        self.janela_h = QSpinBox()
        self.janela_h.setRange(6, 720)
        self.janela_h.setSingleStep(6)
        self.janela_h.setSuffix(" h")
        self.janela_h.setValue(int(cfg.get("janela_horas") or api.JANELA // 3600))
        self.janela_h.setToolTip(
            "Quanto de conversa entra na memória ao abrir. O resto vem ao rolar."
        )
        form.addRow("Carregar as últimas", self.janela_h)

        self.cache_gb = QDoubleSpinBox()
        self.cache_gb.setRange(0.1, 50.0)
        self.cache_gb.setSingleStep(0.5)
        self.cache_gb.setDecimals(1)
        self.cache_gb.setSuffix(" GB")
        # 0.5 GB e' folgado para o que este teto cobre de verdade: uma foto por
        # contato (~5k contatos = ~200MB) mais as respostas cacheadas. Com o
        # default antigo de 4.0 o teto nunca era alcancado — botao morto.
        self.cache_gb.setValue(float(cfg.get("cache_gb") or 0.5))
        self.cache_gb.setToolTip(
            "Teto do cache (lista, contatos, fotos). A mídia recebida NÃO entra "
            "aqui — ela é acervo e não é apagada."
        )
        usado = api.cache_tamanho() / 1024**2
        acervo = api.acervo_tamanho() / 1024**2
        form.addRow(f"Cache máximo (agora: {usado:.0f} MB)", self.cache_gb)
        # o acervo e' o que realmente cresce; sem este numero ninguem descobre
        # o tamanho dele antes do disco encher.
        form.addRow("Mídia baixada (acervo, nunca apagada)", QLabel(f"{acervo:.0f} MB"))

        self.notificar = QCheckBox("Mostrar notificações no desktop")
        self.notificar.setChecked(bool(cfg.get("notificar")))
        form.addRow("Notificações", self.notificar)

        self.tema = QComboBox()
        for nome in PALETAS:
            self.tema.addItem(f"{EMOJI_TEMA.get(nome, '🎨')}  {nome}", nome)
        atual = cfg.get("tema", "whatsapp escuro")
        i = self.tema.findData(atual)
        self.tema.setCurrentIndex(i if i >= 0 else 0)
        form.addRow("Tema", self.tema)

        aviso = QLabel(
            "Notificações vêm desligadas; conversas silenciadas nunca notificam. "
            "Postgres é usado só pela busca global. "
            "A apikey fica em ~/.config/evolution-client/config.toml, modo 600."
        )
        aviso.setWordWrap(True)
        aviso.setStyleSheet(f"color:{P['fraco']}; font-size:12px;")

        botoes = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        botoes.accepted.connect(self.accept)
        botoes.rejected.connect(self.reject)

        caixa = QVBoxLayout(self)
        caixa.addLayout(form)
        caixa.addWidget(aviso)
        caixa.addWidget(botoes)

    def valores(self) -> dict:
        novo = dict(self.cfg)
        novo.update({k: c.text().strip() for k, c in self.entradas.items() if c.text().strip()})
        novo["tema"] = self.tema.currentData() or "whatsapp escuro"
        novo["notificar"] = self.notificar.isChecked()
        novo["janela_horas"] = self.janela_h.value()
        novo["cache_gb"] = self.cache_gb.value()
        return novo


# --------------------------------------------------------------------------- janela


_TEXTURA_CACHE: dict[tuple, QPixmap] = {}


def _tile_textura() -> QPixmap:
    """Tile 220px de doodles sutis (estilo do papel de parede do WhatsApp Web),
    cacheado pelas cores do tema — trocar de tema gera um tile novo."""
    chave = (P["fundo"], P["texto"])
    if chave not in _TEXTURA_CACHE:
        tile = QPixmap(220, 220)
        tile.fill(Qt.transparent)
        p = QPainter(tile)
        p.setRenderHint(QPainter.Antialiasing)
        cor = QColor(P["texto"])
        cor.setAlpha(30)  # sutil de proposito: e' textura, nao desenho
        p.setPen(QPen(cor, 1.4))
        p.setBrush(Qt.NoBrush)

        def coracao(x, y, e):
            caminho = QPainterPath()
            caminho.moveTo(x, y + e * 0.7)
            caminho.cubicTo(x - e * 1.2, y - e * 0.4, x - e * 0.6, y - e * 1.6,
                            x, y - e * 0.4)
            caminho.cubicTo(x + e * 0.6, y - e * 1.6, x + e * 1.2, y - e * 0.4,
                            x, y + e * 0.7)
            p.drawPath(caminho)

        def estrela(cx, cy, r):
            caminho = QPainterPath()
            for i in range(10):
                raio = r if i % 2 == 0 else r * 0.45
                ang = -90 + i * 36
                x = cx + raio * math.cos(math.radians(ang))
                y = cy + raio * math.sin(math.radians(ang))
                if i == 0:
                    caminho.moveTo(x, y)
                else:
                    caminho.lineTo(x, y)
            caminho.closeSubpath()
            p.drawPath(caminho)

        coracao(58, 62, 12)
        estrela(158, 48, 11)
        p.drawEllipse(38, 168, 16, 16)  # círculo vazado
        # ondinha (~)
        o = QPainterPath()
        o.moveTo(120, 128)
        o.cubicTo(126, 118, 132, 138, 138, 128)
        p.drawPath(o)
        # raio
        r = QPainterPath()
        r.moveTo(176, 176)
        r.lineTo(168, 198)
        r.lineTo(181, 192)
        r.lineTo(172, 210)
        p.drawPath(r)
        p.end()
        _TEXTURA_CACHE[chave] = tile
    return _TEXTURA_CACHE[chave]


class Conversa(QListView):
    """A conversa com papel de parede texturizado — as bolhas rolam por cima
    do tile FIXO, como no Web (o padrão não acompanha o scroll)."""

    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        vp = self.viewport().rect()
        p.fillRect(vp, QColor(P["fundo"]))
        tile = _tile_textura()
        for y in range(vp.top(), vp.bottom() + tile.height(), tile.height()):
            for x in range(vp.left(), vp.right() + tile.width(), tile.width()):
                p.drawPixmap(x, y, tile)
        p.end()
        super().paintEvent(ev)


class Janela(QMainWindow):
    # ponytail: Signal e' o unico marshalling correto de thread->GUI no Qt.
    # QTimer.singleShot chamado de thread sem event loop nao agenda nada.
    _chats_prontos = Signal(list)
    _conversa_pronta = Signal(str, list, int)
    _pagina_pronta = Signal(str, list)
    _midia_pronta = Signal(object, str)
    _foto_pronta = Signal(object, str)
    _na_gui = Signal(object)
    _busca_pronta = Signal(str, list)
    _progresso = Signal(str, int, int, str)
    _contatos_prontos = Signal(dict)
    _aviso = Signal(str)
    _gif_baixado = Signal(str)
    _picos_prontos = Signal(object, object)  # (msg, picos|None)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = dict(cfg)
        self.api = api.Api(cfg)
        self.setWindowTitle("zap-evo")
        self.resize(1100, 720)

        self._todos: list[dict] = []
        self.citando: dict | None = None
        self._pendentes: list[dict] = []
        self._ultima_presenca = 0.0
        # spec 2.0: estado do subtítulo do header (fonte única em atualizar_subtitulo)
        self._online_1a1 = False
        self._presenca_efemera: tuple[str, float, str] | None = None
        self._grupo_resumo = ""
        self._grupo_travado = False
        self._ancorado = True
        # Nascem aqui, nao so' no abrir_chat: o botao ↓ e' repintado por evento
        # de rolagem, que pode disparar ANTES da primeira conversa abrir. Sem
        # isto, _atualizar_botao_descer lancava AttributeError a cada rolagem
        # em repouso — engolido pelo Qt, invisivel, mas o botao nunca pintava.
        self._badge_naolidas = 0
        self._ix_ultima_naolida = None
        self._achados: list[str] = []   # IDs, nao posicoes — ver buscar_na_conversa
        self._achado_atual = 0
        self.ocultos = api.carregar_ocultos()
        self.arquivadas = api.carregar_arquivadas()
        self.fixados = api.carregar_fixados()
        self.apelidos = api.carregar_apelidos()
        self.mudos = api.carregar_silenciados()
        self.favoritas = api.carregar_favoritas()
        self.chats, self.msgs = ChatList(), Mensagens()

        self.busca = QLineEdit()
        self.busca.setPlaceholderText("🔍  Buscar conversa")
        self.busca.setClearButtonEnabled(True)
        self.busca.textChanged.connect(self.filtrar)

        self.filtro = "tudo"
        self.abas = QHBoxLayout()
        self.abas.setSpacing(6)
        self.botoes_filtro: dict[str, QPushButton] = {}
        for chave, rotulo in [("tudo", "Tudo"), ("naolidas", "Não lidas"),
                              ("grupos", "Grupos"), ("fixadas", "Fixadas"),
                              ("arquivadas", "🗄  Arquivadas")]:
            b = QPushButton(rotulo)
            b.setCheckable(True)
            b.setProperty("abaFiltro", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=chave: self.trocar_filtro(c))
            self.botoes_filtro[chave] = b
            self.abas.addWidget(b)
        self.abas.addStretch(1)

        self.resultados = QListWidget()
        self.resultados.setMaximumHeight(190)
        self.resultados.setVisible(False)
        self.resultados.itemClicked.connect(self.abrir_resultado)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self.buscar_mensagens)

        self._jid_clicado = None
        # conversa fechada fica na memória por VALIDADE_CACHE e depois some.
        # Voltar em seguida é instantâneo; esquecer libera o heap.
        self._cache: dict[str, dict] = {}
        self._faxina = QTimer(self)
        self._faxina.timeout.connect(self._limpar_cache)
        self._faxina.start(ROTACAO_MS)
        self._carregando: dict[str, tuple[int, int, str]] = {}
        self._midias_alvo = 0
        self._midias_feitas = 0
        self._ordem_suja = False
        self._debounce_ordem = QTimer(self)
        self._debounce_ordem.setSingleShot(True)
        self._debounce_ordem.timeout.connect(self._reordenar_agora)

        self.lista = QListView()
        self.lista.setObjectName("chats")
        self.lista.setModel(self.chats)
        self.lista.setItemDelegate(ItemChat())
        self.lista.setUniformItemSizes(True)
        self.lista.setMouseTracking(True)
        self.lista.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lista.clicked.connect(self.abrir_chat)
        self.lista.viewport().installEventFilter(self)
        self.lista.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lista.customContextMenuRequested.connect(self.menu_do_chat)

        self.conversa = Conversa()
        self.conversa.setObjectName("conversa")
        # o fundo é pintado no paintEvent (cor + textura); o QSS precisa ficar
        # transparente para não encobrir o padrão
        self.conversa.setStyleSheet(
            "QListView { background: transparent; border: none; outline: none; }"
        )
        self.conversa.setModel(self.msgs)
        self.conversa.setItemDelegate(Balao())
        self.conversa.setVerticalScrollMode(QListView.ScrollPerPixel)
        # O WhatsApp Web NAO tem mensagem "selecionada": clicar numa bolha (ou
        # no vazio ao lado dela) nao acende faixa nenhuma. Com SingleSelection o
        # QListView pintava a linha inteira, largura toda do viewport, a cada
        # clique no fundo. Pular ate' uma mensagem usa o realce temporario.
        self.conversa.setSelectionMode(QListView.NoSelection)
        self.conversa.setWordWrap(True)
        self.conversa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.conversa.setSpacing(0)
        self.conversa.doubleClicked.connect(self.abrir_anexo)
        self.conversa.verticalScrollBar().valueChanged.connect(self.talvez_paginar)
        self.conversa.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conversa.customContextMenuRequested.connect(self.menu_da_mensagem)
        self.conversa.viewport().installEventFilter(self)
        self.conversa.setMouseTracking(True)  # ➦ de hover por mensagem

        # botão flutuante ↓ (âncora de não lidas / "pular para o fim"), como o
        # do Web ao abrir conversa com mensagens novas
        self.botao_descer = QPushButton("↓", self.conversa.viewport())
        tam = ESCALA.e(44)
        self.botao_descer.setFixedSize(tam, tam)
        cor = P["dele"]
        fg = P["texto"]
        self.botao_descer.setStyleSheet(
            f"QPushButton {{ background:{cor}; color:{fg}; border:none;"
            f"border-radius:{tam // 2}px; font-size:{ESCALA.px() - 1}px; }}"
            f"QPushButton:hover {{ background:{_mix(cor, fg, 0.15)}; }}"
        )
        self.botao_descer.setCursor(Qt.PointingHandCursor)
        self.botao_descer.setFocusPolicy(Qt.NoFocus)
        self.botao_descer.setToolTip("Pular para a mensagem mais recente")
        self.botao_descer.hide()
        self.botao_descer.clicked.connect(self._descer_rapido)
        self.badge_descer = QLabel(self.botao_descer)
        tb = ESCALA.e(19)
        self.badge_descer.setFixedSize(tb, tb)
        self.badge_descer.setAlignment(Qt.AlignCenter)
        self.badge_descer.setStyleSheet(
            f"QLabel {{ background:#e5484d; color:white; border-radius:{tb // 2}px;"
            f"font-size:{max(7, ESCALA.px() - 6)}px; font-weight:bold; }}"
        )
        self.badge_descer.move(tam - tb // 2, 2)
        self.badge_descer.hide()

        self.avatar_topo = QLabel()
        self.avatar_topo.setFixedSize(ESCALA.e(40), ESCALA.e(40))
        self.avatar_topo.setAlignment(Qt.AlignCenter)
        self.titulo = QLabel("Selecione uma conversa")
        self.titulo.setObjectName("titulo")
        self.subtitulo = QLabel("")
        self.subtitulo.setStyleSheet(
            f"color:{P['fraco']}; font-size:{ESCALA.px()-2}px; background:transparent;"
        )

        self.campo = Composer()
        self.campo.enviar.connect(self.enviar_texto)
        self.campo.arquivos.connect(self.enviar_arquivos)
        self.campo.editar_ultima.connect(self.editar_ultima_enviada)
        self.campo.textChanged.connect(self.digitando)

        # ponytail: instanciar o Gravador sobe ~100MB de backend multimidia.
        # So' nasce quando voce clicar no microfone.
        global AUDIO
        if AUDIO is None:
            AUDIO = AudioInline(self)
        self.audio = AUDIO
        self.audio.mudou.connect(self.conversa.viewport().update)
        self._gravador: Gravador | None = None
        self.pulso_ptt = QTimer(self)
        self.pulso_ptt.timeout.connect(
            lambda: self.statusBar().showMessage(
                f"● gravando  {self.gravador.duracao() // 1000}s — clique no ⏹ para enviar"
            )
        )
        anexar = QPushButton("📎")
        anexar.setFixedWidth(40)
        anexar.setToolTip("Anexar")
        menu_anexo = QMenu(anexar)
        menu_anexo.addAction("📄  Arquivo ou imagem").triggered.connect(self.escolher_arquivo)
        menu_anexo.addAction("🏷  Figurinha").triggered.connect(self.enviar_sticker)
        menu_anexo.addAction("📍  Localização").triggered.connect(self.enviar_local)
        menu_anexo.addAction("👤  Contato").triggered.connect(self.enviar_contato)
        anexar.setMenu(menu_anexo)

        self.b_mic = QPushButton("🎤")
        self.b_mic.setFixedWidth(40)
        self.b_mic.setToolTip("Gravar áudio (clique para começar, clique de novo para enviar)")
        self.b_mic.clicked.connect(self.alternar_ptt)

        self.b_cam = QPushButton("📷")
        self.b_cam.setFixedWidth(40)
        self.b_cam.setToolTip("Câmera: foto, vídeo ou vídeo-recado")
        self.b_cam.clicked.connect(self.abrir_camera)

        self.b_emoji = QPushButton("😊")
        self.b_emoji.setFixedWidth(40)
        self.b_emoji.setToolTip("Emojis")
        self.b_emoji.clicked.connect(self.abrir_emoji)
        self._pop_emoji: SeletorEmoji | None = None

        self.b_gif = QPushButton("GIF")
        self.b_gif.setFixedWidth(44)
        self.b_gif.setToolTip("Enviar GIF (Giphy)")
        self.b_gif.clicked.connect(self.abrir_gif)
        self._pop_gif: SeletorGif | None = None

        # Config global mora no menu do usuário (faixa da sidebar) — spec 2.0

        self.faixa_resposta = QLabel()
        self.faixa_resposta.setStyleSheet(
            f"background:{P['selecao']}; border-left:3px solid {P['citacao']};"
            f"padding:6px 8px; border-radius:4px;"
        )
        fechar_resp = QPushButton("✕")
        fechar_resp.setFixedWidth(30)
        fechar_resp.clicked.connect(self.cancelar_resposta)
        linha_resp = QHBoxLayout()
        linha_resp.setContentsMargins(10, 6, 10, 0)
        linha_resp.addWidget(self.faixa_resposta, 1)
        linha_resp.addWidget(fechar_resp)
        self.caixa_resposta = QWidget()
        self.caixa_resposta.setLayout(linha_resp)
        self.caixa_resposta.setVisible(False)

        rodape = QHBoxLayout()
        rodape.setContentsMargins(10, 8, 10, 10)
        rodape.setSpacing(6)
        rodape.addWidget(anexar, 0, Qt.AlignBottom)
        rodape.addWidget(self.b_cam, 0, Qt.AlignBottom)
        rodape.addWidget(self.b_emoji, 0, Qt.AlignBottom)
        rodape.addWidget(self.b_gif, 0, Qt.AlignBottom)
        rodape.addWidget(self.campo)
        rodape.addWidget(self.b_mic, 0, Qt.AlignBottom)

        direita = QVBoxLayout()
        direita.setContentsMargins(0, 0, 0, 0)
        direita.setSpacing(0)
        textos = QVBoxLayout()
        textos.setSpacing(1)
        textos.setContentsMargins(0, 0, 0, 0)
        textos.addStretch(1)
        textos.addWidget(self.titulo)
        textos.addWidget(self.subtitulo)
        textos.addStretch(1)

        # spec 2.0: zona clicável pontual (avatar + nome), não a barra inteira
        self.zona_nome = QWidget()
        self.zona_nome.setObjectName("zona_nome")
        self.zona_nome.setLayout(textos)
        self.zona_nome.setCursor(Qt.PointingHandCursor)
        self.avatar_topo.setCursor(Qt.PointingHandCursor)

        self.btn_busca = QPushButton("🔍")
        self.btn_busca.setObjectName("btn_busca")
        self.btn_busca.setFixedSize(36, 36)
        self.btn_busca.setToolTip("Pesquisar nesta conversa (Ctrl+Shift+F)")
        self.btn_busca.clicked.connect(self.abrir_busca_na_conversa)
        self.btn_menu = QPushButton("⋮")
        self.btn_menu.setObjectName("btn_menu")
        self.btn_menu.setFixedSize(36, 36)
        self.btn_menu.setToolTip("Menu da conversa")
        self.btn_menu.clicked.connect(self.mostrar_menu_conversa)

        cabecalho = QHBoxLayout()
        cabecalho.setSpacing(ESCALA.e(12))
        cabecalho.addWidget(self.avatar_topo, 0, Qt.AlignVCenter)
        cabecalho.addWidget(self.zona_nome, 1)
        cabecalho.addWidget(self.btn_busca, 0, Qt.AlignVCenter)
        cabecalho.addWidget(self.btn_menu, 0, Qt.AlignVCenter)
        self._cabecalho_layout = cabecalho  # reaplicado no _reestilizar (zoom/tema)

        self.barra_topo = QWidget()
        self.barra_topo.setObjectName("barra_topo")
        self.barra_topo.setLayout(cabecalho)
        self.barra_topo.setFixedHeight(ESCALA.e(56))
        self.avatar_topo.installEventFilter(self)
        self.zona_nome.installEventFilter(self)
        direita.addWidget(self.barra_topo)
        self.carregando = QProgressBar()
        self.carregando.setTextVisible(False)
        self.carregando.setFixedHeight(ESCALA.e(3))
        self.carregando.setVisible(False)
        self.etapa = QLabel("")
        self.etapa.setVisible(False)
        self.etapa.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.etapa.setFixedHeight(ESCALA.e(16))
        faixa_prog = QVBoxLayout()
        faixa_prog.setContentsMargins(0, 0, ESCALA.e(10), 0)
        faixa_prog.setSpacing(0)
        faixa_prog.addWidget(self.carregando)
        faixa_prog.addWidget(self.etapa)
        self.caixa_progresso = QWidget()
        self.caixa_progresso.setLayout(faixa_prog)

        self.busca_chat = QLineEdit()
        self.busca_chat.setPlaceholderText("Pesquisar nesta conversa…")
        self.busca_chat.returnPressed.connect(lambda: self.navegar_achado(1))
        self.busca_chat.textChanged.connect(self.buscar_na_conversa)
        self.contador_achados = QLabel("")
        self.contador_achados.setStyleSheet(f"color:{P['fraco']}; background:transparent;")
        b_ant = QPushButton("↑")
        b_ant.setFixedWidth(30)
        b_ant.clicked.connect(lambda: self.navegar_achado(-1))
        b_prox = QPushButton("↓")
        b_prox.setFixedWidth(30)
        b_prox.clicked.connect(lambda: self.navegar_achado(1))
        b_fecha = QPushButton("✕")
        b_fecha.setFixedWidth(30)
        b_fecha.clicked.connect(self.fechar_busca_na_conversa)
        linha_busca = QHBoxLayout()
        linha_busca.setContentsMargins(10, 6, 10, 6)
        linha_busca.addWidget(self.busca_chat, 1)
        linha_busca.addWidget(self.contador_achados)
        linha_busca.addWidget(b_ant)
        linha_busca.addWidget(b_prox)
        linha_busca.addWidget(b_fecha)
        self.caixa_busca_chat = QWidget()
        self.caixa_busca_chat.setLayout(linha_busca)
        self.caixa_busca_chat.setVisible(False)
        direita.addWidget(self.caixa_busca_chat)

        direita.addWidget(self.caixa_progresso)
        direita.addWidget(self.conversa, 1)
        direita.addWidget(self.caixa_resposta)
        direita.addLayout(rodape)
        painel = QWidget()
        painel.setLayout(direita)

        # spec 2.0: repouso = painel direito inteiro vira placeholder (como o Web)
        self.placeholder = QLabel("Selecione uma conversa para começar")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(
            f"background:{P['fundo']}; color:{P['fraco']};"
        )
        self.stack_direita = QStackedWidget()
        self.stack_direita.addWidget(painel)
        self.stack_direita.addWidget(self.placeholder)
        self.stack_direita.setCurrentIndex(1)  # começa em repouso

        # faixa do usuário no topo da sidebar (spec 2.0): a Config global mora
        # no menu dela — saiu do header da conversa, como no WhatsApp Web
        self.avatar_usuario = QPushButton()
        self.avatar_usuario.setObjectName("avatar_usuario")
        self.avatar_usuario.setToolTip("Menu do usuário")
        self.avatar_usuario.clicked.connect(self.mostrar_menu_usuario)
        self.nome_usuario = QLabel("")
        self.nome_usuario.setObjectName("nome_usuario")
        faixa_linha = QHBoxLayout()
        faixa_linha.setSpacing(ESCALA.e(10))
        faixa_linha.addWidget(self.avatar_usuario)
        faixa_linha.addWidget(self.nome_usuario, 1)
        self._faixa_layout = faixa_linha  # reaplicado no _reestilizar (zoom/tema)
        self.faixa_usuario = QWidget()
        self.faixa_usuario.setObjectName("faixa_usuario")
        self.faixa_usuario.setLayout(faixa_linha)
        self.faixa_usuario.setFixedHeight(ESCALA.e(56))

        esquerda = QVBoxLayout()
        esquerda.setContentsMargins(8, 0, 8, 0)
        esquerda.setSpacing(0)
        esquerda.addWidget(self.faixa_usuario)
        corpo_esq = QVBoxLayout()
        corpo_esq.setContentsMargins(0, 8, 0, 0)
        corpo_esq.setSpacing(6)
        corpo_esq.addWidget(self.busca)
        corpo_esq.addLayout(self.abas)
        corpo_esq.addWidget(self.resultados)
        corpo_esq.addWidget(self.lista, 1)
        self.mini = MiniPlayer(self.audio)
        corpo_esq.addWidget(self.mini)
        esquerda.addLayout(corpo_esq, 1)
        painel_esq = QWidget()
        painel_esq.setLayout(esquerda)

        div = QSplitter()
        div.addWidget(painel_esq)
        div.addWidget(self.stack_direita)
        div.setSizes([340, 760])
        div.setHandleWidth(1)
        self.setCentralWidget(div)
        self.statusBar().showMessage("carregando…")

        self._chats_prontos.connect(self._pintar_chats)
        self._conversa_pronta.connect(self._pintar_conversa)
        self._pagina_pronta.connect(self._pintar_pagina)
        self._midia_pronta.connect(self._pintar_midia)
        if ANIMA:
            ANIMA.quadro.connect(self._repintar_gif)
        self._picos_prontos.connect(self._pintar_picos)
        self._foto_pronta.connect(self._pintar_foto)
        self._na_gui.connect(lambda fn: fn())
        self._busca_pronta.connect(self._pintar_busca)
        self._progresso.connect(self._pintar_progresso)
        self._contatos_prontos.connect(self._guardar_contatos)
        self._aviso.connect(self.statusBar().showMessage)

        self._reestilizar()

        self.eventos = Eventos(cfg)
        self.eventos.mensagem.connect(self.mensagem_recebida)
        self.eventos.apagou.connect(self.marcar_apagada)
        self.eventos.atualizou.connect(self.atualizar_status)
        self.eventos.presenca.connect(self.mostrar_presenca)
        self.eventos.chats_mudaram.connect(self.sincronizar_chats)
        self.eventos.conexao.connect(self.mostrar_conexao)
        if me := (self.api.me or "").strip():
            EU_JIDS.update({me, f"{me}@s.whatsapp.net"})
        POOL.submit(self.eventos.ligar)
        POOL.submit(self._carregar_chats)
        POOL.submit(self._carregar_usuario)

        # Atalhos do WhatsApp Web (pesquisado em 09/09/2026 — faq.whatsapp.com
        # + 3 fontes independentes cruzadas: xda-developers, wwebcustomizer,
        # canaltech/tecnoblog). Web sempre usa Ctrl+Alt+<tecla>; Shift extra
        # quando há uma variante (ex.: Ctrl+Alt+E arquiva, Ctrl+Alt+Shift+E
        # ficaria livre). Confirmado em ≥2 fontes cada um destes:
        for combo, acao in [
            ("Ctrl+Alt+N", self.nova_conversa),          # nova conversa
            ("Ctrl+Alt+Shift+]", lambda: self.pular_chat(1)),
            ("Ctrl+Alt+Shift+[", lambda: self.pular_chat(-1)),
            ("Ctrl+Alt+Shift+P", self.fixar_atual),
            ("Ctrl+Alt+E", self.arquivar_atual),
            ("Ctrl+Alt+Shift+M", self.silenciar_atual),  # silenciar/mudo
            ("Ctrl+Alt+Shift+U", self.nao_lida_atual),
            ("Ctrl+Alt+P", self.abrir_meu_perfil),        # meu perfil
            ("Ctrl+Alt+,", self.abrir_config),
            ("Ctrl+Alt+/", lambda: self.busca.setFocus()),
            ("Ctrl+Alt+Shift+F", self.abrir_busca_na_conversa),  # buscar NESTA conversa
            ("Ctrl+Shift+F", self.abrir_busca_na_conversa),      # variante sem Alt: fontes discordaram
            ("Ctrl+Alt+I", self.abrir_dados_do_contato),
            ("Ctrl+M", self.nova_conversa),  # atalho antigo daqui: mantido, nao custa nada
            ("Ctrl+=", lambda: self.zoom(+1)),
            ("Ctrl++", lambda: self.zoom(+1)),
            ("Ctrl+Shift+=", lambda: self.zoom(+1)),
            ("Ctrl+-", lambda: self.zoom(-1)),
            ("Ctrl+0", lambda: self.zoom(0)),
            ("Esc", self.sair_da_conversa),
        ]:
            QShortcut(QKeySequence(combo), self, activated=acao)

        # ponytail: RSS a cada 30min. Subiu monotonicamente por 20h = vazou,
        # igual ao ZapZap. E' este log que diz se o projeto atingiu o objetivo.
        self.relogio = QTimer(self)
        self.relogio.timeout.connect(
            lambda: log.info(
                "rss=%dMB · acervo=%dMB · cache=%dMB",
                rss_mb(), api.acervo_tamanho() // 1024**2, api.cache_tamanho() // 1024**2,
            )
        )
        self.relogio.start(30 * 60 * 1000)

    # ---------- carregamento ----------

    def zoom(self, delta: int):
        """Ctrl+= aumenta, Ctrl+- diminui, Ctrl+0 volta ao padrão."""
        if delta == 0:
            mudou, ESCALA.valor = ESCALA.valor != ESCALA.BASE, ESCALA.BASE
        else:
            mudou = ESCALA.mudar(delta)
        if not mudou:
            return
        documento.cache_clear()
        foto.cache_clear()
        avatar.cache_clear()
        QApplication.instance().setStyleSheet(tema_css())
        self.avatar_topo.setFixedSize(ESCALA.e(40), ESCALA.e(40))
        self._reestilizar()
        self.lista.reset()
        self.conversa.reset()
        self.statusBar().showMessage(f"escala: {ESCALA.px()}px")
        self.cfg["escala"] = ESCALA.valor
        # Sincrono de proposito: e' um tmp+replace de ~300 bytes, imperceptivel.
        # No POOL, dois zooms seguidos viravam dois escritores concorrentes no
        # mesmo arquivo, e sair no meio perdia a gravacao.
        api.salvar_config(dict(self.cfg))

    def sair_da_conversa(self):
        """Esc: fecha a busca se estiver aberta; senão volta ao repouso."""
        if self.caixa_busca_chat.isVisible():
            self.fechar_busca_na_conversa()
            return
        if self.citando:
            self.cancelar_resposta()
            return
        if self.busca.text():
            self.busca.clear()
            return
        if not self.msgs.jid:
            self.lista.setFocus()
            return
        self._guardar_no_cache()
        self.msgs.abrir("", [], 1)
        self.msgs.jids = []
        self.titulo.setText("Selecione uma conversa")
        self.subtitulo.setText("")
        self.avatar_topo.clear()
        self.lista.clearSelection()
        self.lista.setCurrentIndex(QModelIndex())
        self.campo.clear()
        self.statusBar().showMessage("em repouso — escolha uma conversa")
        self.stack_direita.setCurrentIndex(1)  # repouso: placeholder no lugar
        self.lista.setFocus()

    def trocar_filtro(self, chave: str):
        self.filtro = chave
        self.aplicar_filtros()

    def _passa(self, chat: dict) -> bool:
        jids = chat.get("_jids") or [chat.get("remoteJid")]
        arquivado = any(j in self.arquivadas for j in jids)
        if self.filtro == "arquivadas":
            return arquivado
        if arquivado:
            return False  # o Web não mistura arquivadas na lista principal
        if self.filtro == "naolidas":
            return bool(chat.get("unreadCount"))
        if self.filtro == "grupos":
            return (chat.get("remoteJid") or "").endswith("@g.us")
        if self.filtro == "fixadas":
            return bool(chat.get("_fixado"))
        return True

    def aplicar_filtros(self):
        selecionado = None
        if (ix := self.lista.currentIndex()).isValid():
            selecionado = (ix.data(Qt.UserRole) or {}).get("remoteJid")
        # beginResetModel repinta barato (só as linhas visíveis), mas descarta
        # seleção e posição da barra — as duas voltam à mão.
        rolagem = self.lista.verticalScrollBar().value()
        termo = self.busca.text().strip().lower()
        visiveis = [c for c in self._todos if self._passa(c)]
        if termo:
            visiveis = [
                c for c in visiveis
                if termo in api.nome_do_chat(c, self.chats.contatos).lower()
                or termo in api.text_of(c.get("lastMessage") or {}).lower()
            ]
        self.chats.definir(visiveis)
        self.lista.verticalScrollBar().setValue(rolagem)
        if selecionado:  # reordenar nao pode tirar a conversa aberta da selecao
            for i, c in enumerate(visiveis):
                if c.get("remoteJid") == selecionado:
                    self.lista.setCurrentIndex(self.chats.index(i))
                    break
        for chave, b in self.botoes_filtro.items():
            b.setChecked(chave == self.filtro)
        contagens = {
            "tudo": sum(1 for c in self._todos
                        if not any(j in self.arquivadas
                                   for j in (c.get("_jids") or [c.get("remoteJid")]))),
            "naolidas": sum(1 for c in self._todos if c.get("unreadCount")),
            "grupos": sum(1 for c in self._todos if (c.get("remoteJid") or "").endswith("@g.us")),
            "fixadas": sum(1 for c in self._todos if c.get("_fixado")),
            "arquivadas": sum(1 for c in self._todos
                              if any(j in self.arquivadas
                                     for j in (c.get("_jids") or [c.get("remoteJid")]))),
        }
        for chave, rotulo in [("tudo", "Tudo"), ("naolidas", "Não lidas"),
                              ("grupos", "Grupos"), ("fixadas", "Fixadas"),
                              ("arquivadas", "🗄  Arquivadas")]:
            n = contagens[chave]
            self.botoes_filtro[chave].setText(f"{rotulo} {n}" if n and chave != "tudo" else rotulo)

    def filtrar(self, termo: str):
        self.aplicar_filtros()
        if not termo.strip():
            self.resultados.setVisible(False)
            return
        self._debounce.start(400)

    def buscar_mensagens(self):
        termo = self.busca.text().strip()
        if len(termo) < 3:
            self.resultados.setVisible(False)
            return

        def trabalho():
            # duas ondas: a janela recente volta em milissegundos e cobre a
            # maioria das buscas; o histórico antigo chega depois, sem travar.
            try:
                recentes = self.api.buscar_recente(termo)
            except LookupError:
                self._busca_pronta.emit(termo, [])
                return
            except Exception as e:
                self._aviso.emit(str(e))
                return
            self._busca_pronta.emit(termo, recentes)
            if termo != self.busca.text().strip():
                return  # o usuário já mudou o que procura
            try:
                antigas = self.api.buscar_antigas(termo)
            except Exception as e:
                # a 1a onda ja' pintou: sem aviso, "so' recentes" passa por
                # "nao existe mais nada", que e' outra coisa.
                self._aviso.emit(f"busca no histórico antigo falhou: {e}")
                return
            if antigas and termo == self.busca.text().strip():
                self._busca_pronta.emit(termo, recentes + antigas)

        POOL.submit(trabalho)

    def _cabecalho(self, texto: str) -> QListWidgetItem:
        item = QListWidgetItem(texto)
        item.setFlags(Qt.NoItemFlags)
        item.setForeground(QColor(P["fraco"]))
        item.setFont(fonte(self.font(), -2, negrito=True))
        return item

    def _pintar_busca(self, termo: str, achados: list):
        """Resultados em seções, como no WhatsApp Web: Conversas e Mensagens."""
        self.resultados.clear()
        baixo = termo.lower()
        conversas = [
            c for c in self._todos
            if baixo in api.nome_do_chat(c, self.chats.contatos).lower()
        ][:8]

        if conversas:
            self.resultados.addItem(self._cabecalho(f"CONVERSAS  ({len(conversas)})"))
            for c in conversas:
                item = QListWidgetItem(f"  {api.nome_do_chat(c, self.chats.contatos)}")
                item.setData(Qt.UserRole, c.get("remoteJid"))
                self.resultados.addItem(item)

        if achados:
            corte = datetime.now().timestamp() - api.JANELA
            recentes = [m for m in achados if m.get("messageTimestamp", 0) >= corte]
            antigas = [m for m in achados if m.get("messageTimestamp", 0) < corte]
            self.resultados.addItem(self._cabecalho(
                f"MENSAGENS  ({len(recentes)} nas últimas 72h"
                + (f" · {len(antigas)} antes)" if antigas else ")")
            ))
            nomes = {}
            for c in self._todos:
                for j in c.get("_jids") or [c.get("remoteJid")]:
                    nomes[j] = api.nome_do_chat(c, self.chats.contatos)
            for m in achados[:60]:
                jid = (m.get("key") or {}).get("remoteJid", "")
                onde = nomes.get(jid) or api.formatar_numero(jid)
                quando = datetime.fromtimestamp(
                    m.get("messageTimestamp", 0)
                ).strftime("%d/%m")
                item = QListWidgetItem(f"  {onde} · {quando} — {api.text_of(m)[:52]}")
                item.setData(Qt.UserRole, jid)
                self.resultados.addItem(item)
        elif not conversas:
            if not self.api.pg:
                self.resultados.addItem(
                    self._cabecalho("configure o Postgres no ⚙ para buscar no histórico")
                )
            else:
                self.resultados.setVisible(False)
                return

        self.resultados.setVisible(True)
        self.resultados.setMaximumHeight(260)
        self.statusBar().showMessage(
            f"{len(conversas)} conversas e {len(achados)} mensagens com “{termo}”"
        )

    def abrir_resultado(self, item):
        jid = item.data(Qt.UserRole)
        if not jid:
            return
        for i, c in enumerate(self.chats.itens):
            if jid in (c.get("_jids") or [c.get("remoteJid")]):
                ix = self.chats.index(i)
                self.lista.setCurrentIndex(ix)
                self.abrir_chat(ix)
                return
        self.statusBar().showMessage("conversa não está na lista carregada")

    def abrir_config(self):
        dlg = Config(self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        novo = dlg.valores()
        api.salvar_config(novo)
        trocou_tema = novo.get("tema") != self.cfg.get("tema", "escuro")
        conexao_mudou = any(
            novo.get(k) != self.cfg.get(k) for k in ("host", "apikey", "instance")
        )
        self.cfg = novo
        if trocou_tema:
            self.aplicar_tema(novo["tema"])
        if conexao_mudou:
            self.statusBar().showMessage(
                "conexão alterada — reinicie o app para valer (uv run main.py)"
            )

    def aplicar_tema(self, nome: str):
        P.update(PALETAS.get(nome, PALETAS["whatsapp escuro"]))
        foto.cache_clear()
        avatar.cache_clear()
        documento.cache_clear()
        cor_autor.cache_clear()
        QApplication.instance().setStyleSheet(tema_css())
        self._reestilizar()
        self.lista.viewport().update()
        self.conversa.viewport().update()

    def _reestilizar(self):
        """Widgets com estilo próprio não são alcançados pelo stylesheet global.

        Ficavam com a cor do tema em que foram criados — o cabeçalho continuava
        escuro depois de trocar para um tema claro.
        """
        # spec 2.0: sem linha sob o header — a separação é o tom (painel→fundo),
        # como no Web. Alturas/margens reaplicadas porque zoom() passa por aqui.
        self.barra_topo.setFixedHeight(ESCALA.e(56))
        self.faixa_usuario.setFixedHeight(ESCALA.e(56))
        self._cabecalho_layout.setContentsMargins(
            ESCALA.e(15), ESCALA.e(6), ESCALA.e(11), ESCALA.e(6)
        )
        self._cabecalho_layout.setSpacing(ESCALA.e(12))
        self._faixa_layout.setContentsMargins(0, 0, ESCALA.e(6), 0)
        self._faixa_layout.setSpacing(ESCALA.e(10))
        self.placeholder.setStyleSheet(
            f"background:{P['fundo']}; color:{P['fraco']};"
        )
        self.subtitulo.setStyleSheet(
            f"color:{P['fraco']}; font-size:{ESCALA.px() - 2}px; background:transparent;"
        )
        self.faixa_resposta.setStyleSheet(
            f"background:{P['selecao']}; border-left:3px solid {P['citacao']};"
            f" padding:6px 8px; border-radius:4px;"
        )
        self.contador_achados.setStyleSheet(
            f"color:{P['fraco']}; background:transparent;"
        )
        for b in self.botoes_filtro.values():
            b.style().unpolish(b)
            b.style().polish(b)
        self.carregando.setStyleSheet(
            f"QProgressBar {{ background:{P['divisor']}; border:none; }}"
            f"QProgressBar::chunk {{ background:{P['accent']}; }}"
        )
        self.etapa.setStyleSheet(
            f"color:{P['fraco']}; background:{P['painel']};"
            f" font-size:{ESCALA.px() - 3}px; padding:2px;"
        )
        self.mini.reestilizar()

    def _carregar_usuario(self):
        """Avatar e nome do próprio dono para a faixa do usuário (spec 2.0).

        Roda no pool (rede); o QPixmap nasce só na GUI, via _na_gui.
        """
        me = (self.api.me or "").strip()
        jid = f"{me}@s.whatsapp.net" if me else ""
        nome = ""
        caminho = None
        if jid:
            perfil = self.api.perfil(jid)
            nome = (perfil.get("nome") or "").strip()
            caminho = self.api.avatar_file({"remoteJid": jid, "profilePicUrl": ""})
        if not caminho and nome:
            caminho = ""  # sem foto: cai para iniciais

        def pintar():
            if nome:
                self.nome_usuario.setText(nome)
            if caminho:
                self.avatar_usuario.setIcon(QIcon(foto(str(caminho), 40)))
            elif nome:
                self.avatar_usuario.setIcon(QIcon(avatar(nome, me or "eu", 40)))
            else:
                self.avatar_usuario.setText("🙂")
            # a minha foto tambem vai no cartao de voz das MINHAS notas de voz
            self.conversa.itemDelegate().eu = (
                nome or "Você", jid or "eu", str(caminho or "")
            )

        self._na_gui.emit(pintar)

    def _carregar_chats(self):
        # ponytail: pinta o que está em disco na hora e revalida por fora. Sem
        # isto o app abre em branco por ~2,5s toda vez.
        guardados = self.api.chats_do_cache()
        if guardados:
            frios = self.api.contatos_do_cache() or {}
            self._contatos_prontos.emit(frios)
            self._chats_prontos.emit(api.unir_duplicados(guardados, frios))
            self._progresso.emit("", 0, 100, "atualizando")
        try:
            contatos = self.api.contatos()
            unidos = api.unir_duplicados(self.api.chats(), contatos)
            self._contatos_prontos.emit(contatos)
            self._chats_prontos.emit(unidos)
            if guardados:
                self._progresso.emit("", -1, 1, "")
        except Exception as e:
            log.error("findChats falhou: %s", e)
            self._progresso.emit("", -1, 1, "")
            if not guardados:
                self._aviso.emit(f"não consegui carregar as conversas: {e}")
            else:
                self._aviso.emit("offline — mostrando a última lista conhecida")

    def _pintar_chats(self, chats):
        aberta = self.msgs.jid
        for c in chats:
            c["_fixado"] = c.get("remoteJid") in self.fixados
            c["_apelidos"] = self.apelidos
            c["_mudo"] = any(
                api.esta_mudo(self.mudos, j) for j in (c.get("_jids") or [c.get("remoteJid")])
            )
        self._todos = api.ordenar(chats, self.fixados)
        self.aplicar_filtros()
        fixos = len(self.fixados)
        self.statusBar().showMessage(
            f"{len(chats)} conversas" + (f" · {fixos} fixadas" if fixos else "")
            + f" · RSS {rss_mb()} MB"
        )
        if aberta:  # a lista trocou por baixo: mantém a conversa selecionada
            for i, c in enumerate(self.chats.itens):
                if aberta in (c.get("_jids") or [c.get("remoteJid")]):
                    self.lista.setCurrentIndex(self.chats.index(i))
                    break
        for c in chats[:80]:  # ponytail: so o que cabe na tela; o resto ao rolar
            self._buscar_foto(c)

    def _buscar_foto(self, chat):
        if chat.get("_foto"):
            return

        def trabalho():
            if caminho := self.api.avatar_file(chat):
                self._foto_pronta.emit(chat, str(caminho))

        MIDIA.submit(trabalho)

    def _pintar_foto(self, chat, caminho):
        chat["_foto"] = caminho
        for i, c in enumerate(self.chats.itens):
            if c is chat:
                ix = self.chats.index(i)
                self.chats.dataChanged.emit(ix, ix)
                return

    def _chat_atual(self) -> dict | None:
        ix = self.lista.currentIndex()
        return ix.data(Qt.UserRole) if ix.isValid() else None

    def pular_chat(self, passo: int):
        linha = self.lista.currentIndex().row() + passo
        if 0 <= linha < self.chats.rowCount():
            ix = self.chats.index(linha)
            self.lista.setCurrentIndex(ix)
            self.abrir_chat(ix)

    def fixar_atual(self):
        if chat := self._chat_atual():
            self.alternar_fixado(chat.get("remoteJid", ""))

    def arquivar_atual(self):
        if chat := self._chat_atual():
            self._arquivar_chat(chat)

    def _arquivar_chat(self, chat: dict):
        jids = chat.get("_jids") or [chat.get("remoteJid")]
        for j in jids:
            self.arquivadas.add(j)
        api.salvar_arquivadas(self.arquivadas)
        POOL.submit(self._tentar, self.api.arquivar, chat, True)
        self._pintar_chats(self._todos)
        self.statusBar().showMessage("conversa arquivada")

    def _desarquivar_jid(self, jid: str):
        if jid not in self.arquivadas:
            return
        self.arquivadas.discard(jid)
        api.salvar_arquivadas(self.arquivadas)
        self._pintar_chats(self._todos)

    def nao_lida_atual(self):
        if chat := self._chat_atual():
            POOL.submit(self._tentar, self.api.marcar_nao_lida, chat)
            self.statusBar().showMessage("marcada como não lida")

    def menu_do_chat(self, ponto: QPoint):
        ix = self.lista.indexAt(ponto)
        if not ix.isValid():
            return
        if menu := self.menu_para_chat(ix.data(Qt.UserRole)):
            menu.exec(self.lista.viewport().mapToGlobal(ponto))

    def menu_para_chat(self, chat: dict | None) -> QMenu | None:
        """Monta o menu da conversa sem abrir. Separado do menu_do_chat pelo
        mesmo motivo do menu_para_mensagem: o que so' existe dentro de um
        `exec()` nao da' para conferir — e foi assim que o visualizador ficou
        sem acao nenhuma sem ninguem notar."""
        if not chat:
            return None
        jid = chat.get("remoteJid", "")
        menu = QMenu(self)
        fixado = jid in self.fixados
        menu.addAction("Desafixar" if fixado else "📌 Fixar no topo").triggered.connect(
            lambda: self.alternar_fixado(jid)
        )
        if api.esta_mudo(self.mudos, jid):
            menu.addAction("🔔  Reativar som").triggered.connect(
                lambda: self.silenciar(jid, None)
            )
        else:
            sub = menu.addMenu("🔕  Silenciar")
            for rotulo, segundos in api.DURACOES_MUDO:
                sub.addAction(rotulo).triggered.connect(
                    lambda _=False, s=segundos: self.silenciar(jid, s)
                )
        menu.addAction("ℹ  Informações").triggered.connect(lambda: self.info_do_chat())
        arquivado = jid in self.arquivadas
        if arquivado:
            menu.addAction("📂  Desarquivar").triggered.connect(
                lambda: self._desarquivar_jid(jid)
            )
        else:
            menu.addAction("🗄  Arquivar").triggered.connect(lambda: self.arquivar_atual())
        menu.addAction("✎ Renomear contato").triggered.connect(
            lambda: self.renomear(chat)
        )
        # As tres que faltavam para bater com o menu do WhatsApp Web.
        # "Apagar conversa" e "Limpar conversa" NAO entram: conferido na fonte
        # do tag 2.3.7 (chat.router.ts) — nao existe rota para nenhuma das duas.
        menu.addSeparator()
        menu.addAction("✉  Marcar como não lida").triggered.connect(
            lambda: self.marcar_nao_lida(chat)
        )
        if jid.endswith("@g.us"):
            menu.addAction("🚪  Sair do grupo").triggered.connect(
                lambda: self.sair_do_grupo(chat)
            )
        else:
            menu.addAction("⊘  Bloquear contato").triggered.connect(
                lambda: self.bloquear_contato(chat)
            )
        return menu

    def marcar_nao_lida(self, chat: dict):
        """O 'Marcar como não lida' do Web: existia so' no atalho Ctrl+Alt+Shift+U."""
        def feito():
            chat["unreadCount"] = max(1, int(chat.get("unreadCount") or 0))
            self.aplicar_filtros()
            self.statusBar().showMessage("marcada como não lida")
        POOL.submit(self._depois, feito, self.api.marcar_nao_lida, chat)

    def bloquear_contato(self, chat: dict):
        jid = chat.get("remoteJid", "")
        nome = api.nome_do_chat(chat, self.chats.contatos)
        if QMessageBox.question(
            self, "Bloquear",
            f"Bloquear {nome}?\n\nEle deixa de te enviar mensagens e de ver "
            "seu visto por último."
        ) != QMessageBox.Yes:
            return
        POOL.submit(self._depois,
                    lambda: self.statusBar().showMessage(f"{nome} bloqueado"),
                    self.api.bloquear, jid, True)

    def sair_do_grupo(self, chat: dict):
        jid = chat.get("remoteJid", "")
        nome = api.nome_do_chat(chat, self.chats.contatos)
        if QMessageBox.question(
            self, "Sair do grupo",
            f"Sair de “{nome}”?\n\nPara voltar você precisa de um convite novo."
        ) != QMessageBox.Yes:
            return
        def feito():
            self.arquivadas.discard(jid)
            self.statusBar().showMessage(f"você saiu de {nome}")
            POOL.submit(self._carregar_chats)
        POOL.submit(self._depois, feito, self.api.grupo_sair, jid)

    def renomear(self, chat: dict):
        atual = api.nome_do_chat(chat, self.chats.contatos)
        novo, ok = QInputDialog.getText(self, "Renomear contato", "Nome:", text=atual)
        if not ok:
            return
        chave = api.identidade(chat)
        if novo.strip():
            self.apelidos[chave] = novo.strip()
        else:
            self.apelidos.pop(chave, None)
        api.salvar_apelidos(self.apelidos)
        self._pintar_chats(self._todos)
        if chat.get("remoteJid") in (self.msgs.jids or []):
            self.titulo.setText(api.nome_do_chat(chat, self.chats.contatos))

    def silenciar(self, jid: str, segundos: int | None):
        """segundos=None reativa o som; 0 silencia para sempre."""
        if segundos is None:
            self.mudos.pop(jid, None)
        else:
            self.mudos[jid] = 0 if segundos == 0 else int(datetime.now().timestamp()) + segundos
        api.salvar_silenciados(self.mudos)
        self._pintar_chats(self._todos)
        self.atualizar_subtitulo()  # 🔕/🔔 ao vivo no header se a conversa está aberta

    def alternar_mudo(self, jid: str):
        """Atalho: silencia para sempre ou reativa."""
        self.silenciar(jid, None if api.esta_mudo(self.mudos, jid) else 0)

    def alternar_fixado(self, jid: str):
        if jid in self.fixados:
            self.fixados.remove(jid)
        else:
            self.fixados.insert(0, jid)
        api.salvar_fixados(self.fixados)
        self._pintar_chats(self._todos)
        self.atualizar_subtitulo()  # 📌 ao vivo no header se a conversa está aberta

    def abrir_chat(self, ix):
        self._guardar_no_cache()
        chat = ix.data(Qt.UserRole)
        # a lista pode ter reordenado entre o clique e este dispatch
        if alvo := getattr(self, "_jid_clicado", None):
            if (chat or {}).get("remoteJid") != alvo:
                for i, c in enumerate(self.chats.itens):
                    if c.get("remoteJid") == alvo:
                        chat, ix = c, self.chats.index(i)
                        self.lista.setCurrentIndex(ix)
                        break
            self._jid_clicado = None
        if not chat:
            return
        jid = chat["remoteJid"]
        # estado da âncora de não lidas é por conversa; a nova decide sozinha
        self._badge_naolidas = 0
        self._ix_ultima_naolida = None
        self.botao_descer.hide()
        self.msgs.jids = chat.get("_jids") or [jid]
        nome = api.nome_do_chat(chat, self.chats.contatos)
        self.titulo.setText(nome)
        self._barra_da_conversa()
        self.stack_direita.setCurrentIndex(0)  # sai do repouso
        retrato = foto(chat["_foto"], 40) if chat.get("_foto") else QPixmap()
        self.avatar_topo.setPixmap(retrato if not retrato.isNull() else avatar(nome, jid, 40))
        # o cartao de voz mostra a foto de quem falou (como no Web): o delegate
        # nao alcanca a lista de conversas, entao a Janela carimba quem e'
        self.conversa.itemDelegate().contato = (nome, jid, chat.get("_foto") or "")
        # spec 2.0: o subtítulo tem uma fonte de verdade (atualizar_subtitulo),
        # com prioridade do Web — presença > silenciada > fixada > grupo
        self._online_1a1 = False
        self._presenca_efemera = None
        self._grupo_resumo = ""
        self._grupo_travado = False
        if jid.endswith("@g.us"):
            POOL.submit(self._info_do_grupo, jid)
        else:
            self.travar_composer("")
        self.atualizar_subtitulo()

        if (guardada := self._cache.get(jid)) is not None:
            guardada["saiu_em"] = None  # voltou: para de contar para a faxina
            self.msgs.restaurar(jid, guardada)
            self._ancorado = True
            self._descer()
            self.statusBar().showMessage(f"{len(guardada['itens'])} mensagens (da memória)")
            return

        self._progresso.emit(jid, 0, 100, "abrindo")

        def trabalho():
            try:
                registros, paginas, mais = self.api.messages_recentes(
                    self.msgs.jids,
                    janela_s=int(self.cfg.get("janela_horas") or 72) * 3600,
                    progresso=lambda f, a, e: self._progresso.emit(jid, f, a, e),
                )
                self._conversa_pronta.emit(jid, registros, paginas)
                if not mais:
                    self._aviso.emit("início da conversa")
            except Exception as e:
                self._progresso.emit(jid, -1, 1, "")
                self._aviso.emit(f"não consegui abrir a conversa: {e}")

        POOL.submit(trabalho)

    def _pintar_conversa(self, jid, registros, paginas):
        self.statusBar().showMessage(
            f"{len(registros)} mensagens recentes · role para cima para o histórico"
        )
        registros = [
            m for m in registros
            if (m.get("key") or {}).get("id") not in self.ocultos and api.e_visivel(m)
        ]
        for m in registros:
            if (m.get("key") or {}).get("id") in self.favoritas:
                m["_favorita"] = True
        self.msgs.abrir(jid, registros, paginas)
        # marca lida tudo que chegou não lido — a Evolution devolve o status por
        # mensagem; antes só olhava os 30 primeiros registros da página
        naolidas = [
            m["key"] for m in self.msgs.itens
            if not (m.get("key") or {}).get("fromMe") and m.get("status") != "READ"
        ]
        if naolidas:
            POOL.submit(self.api.marcar_lida, naolidas)
        # a prévia da barra lateral pode ter ficado para trás (por exemplo, presa
        # no 🕐 de um envio): adota a última mensagem que acabou de chegar
        if registros:
            recente = max(registros, key=lambda m: m.get("messageTimestamp", 0))
            for c in self._todos:
                if jid in (c.get("_jids") or [c.get("remoteJid")]):
                    antiga = c.get("lastMessage") or {}
                    if antiga.get("messageTimestamp", 0) <= recente.get("messageTimestamp", 0):
                        c["lastMessage"] = recente
                        self.chats.mudou(c)
                    break
        for c in self._todos:
            if jid in (c.get("_jids") or [c.get("remoteJid")]) and c.get("unreadCount"):
                c["unreadCount"] = 0
                self.chats.mudou(c)
                break
        # Web: com não lidas, entra na MAIS ANTIGA delas e mostra o ↓ até o
        # usuário descer; sem não lidas, vai direto ao fim como sempre
        self._ancorar_naolidas()
        for m in registros:
            self._buscar_midia(m)
        # carga concluída (sucesso): a barra de progresso da linha some — antes
        # só o except limpava _carregando, e a barra ficava presa no último
        # estado (100% quando a conversa atingia o teto de mensagens)
        self._progresso.emit(jid, -1, 1, "")

    def talvez_paginar(self, valor):
        barra = self.conversa.verticalScrollBar()
        self._ancorado = valor >= barra.maximum() - 40
        # âncora de não lidas: o scroll programático de abertura não pagina
        if getattr(self, "_ancorando", False):
            return
        self._atualizar_botao_descer(valor)
        if valor > 4 or self.msgs.pagina >= self.msgs.paginas:
            return
        jid, proxima = self.msgs.jid, self.msgs.pagina + 1
        jids = self.msgs.jids or [jid]

        def trabalho():
            try:
                registros, _ = self.api.messages(jids, proxima, PAGINA)
                self._pagina_pronta.emit(jid, registros)
            except Exception as e:
                log.error("paginação falhou: %s", e)
                self._aviso.emit(f"não carregou o histórico: {e}")

        POOL.submit(trabalho)

    def _pintar_pagina(self, jid, registros):
        self._ancorado = False
        registros = [
            m for m in registros
            if (m.get("key") or {}).get("id") not in self.ocultos and api.e_visivel(m)
        ]
        if jid != self.msgs.jid or not registros:
            return
        self.msgs.anteriores(registros)
        for m in registros:
            self._buscar_midia(m)

    # ---------- mídia ----------

    def _buscar_midia(self, msg):
        if not api.media_kind(msg.get("messageType", "")) or msg.get("_arquivo"):
            # já está no disco: mesmo assim a onda real precisa dos picos
            if msg.get("_arquivo") and (msg.get("messageType") or "") == "audioMessage":
                self._picos_do_audio(msg)
            return

        def trabalho():
            if caminho := self.api.media_file(msg):
                self._midia_pronta.emit(msg, str(caminho))
            else:
                # ponteiro vencido no CDN ou rede fora: sem isto a bolha fica
                # vazia para sempre e parece defeito de render.
                self._aviso.emit("mídia indisponível — ponteiro expirado ou sem rede")

        MIDIA.submit(trabalho)

    def _descer(self):
        if self._ancorado:
            self.conversa.scrollToBottom()
            QTimer.singleShot(0, self.conversa.scrollToBottom)

    # ---------- âncora de não lidas (comportamento do Web) ----------

    def _naolidas_no_modelo(self):
        """(index da 1ª não lida, index da última, total) — não lida é recebida
        com status != READ. A âncora do Web entra na MAIS ANTIGA das novas."""
        primeira = ultima = None
        n = 0
        for i, m in enumerate(self.msgs.itens):
            if not (m.get("key") or {}).get("fromMe") and m.get("status") != "READ":
                if primeira is None:
                    primeira = i
                ultima = i
                n += 1
        return primeira, ultima, n

    def _ancorar_naolidas(self):
        primeira, ultima, n = self._naolidas_no_modelo()
        if primeira is None:
            self._ix_ultima_naolida = None
            self._badge_naolidas = 0
            self.conversa.scrollToBottom()
            return
        self._ix_ultima_naolida = ultima
        self._badge_naolidas = n
        self._ancorado = False
        self._ancorando = True
        self.conversa.scrollTo(self.msgs.index(primeira), QListView.PositionAtTop)
        # o scroll programático dispara valueChanged -> talvez_paginar; a flag
        # impede que ele carregue a página anterior sozinho (é do usuário subir)
        QTimer.singleShot(150, lambda: setattr(self, "_ancorando", False))
        self._mostrar_botao_descer()

    def _mostrar_botao_descer(self):
        b = self.botao_descer
        vp = self.conversa.viewport()
        b.move(vp.width() - b.width() - ESCALA.e(18),
               vp.height() - b.height() - ESCALA.e(18))
        self.badge_descer.setText(str(self._badge_naolidas) if self._badge_naolidas else "")
        self.badge_descer.setVisible(bool(self._badge_naolidas))
        b.show()
        b.raise_()
        self.badge_descer.raise_()

    def _atualizar_botao_descer(self, valor: int):
        """Fora do fim mostra o ↓ (há mensagens abaixo). A âncora morre quando
        o usuário passa da última não lida ou chega ao fim — aí some o badge."""
        if not self.msgs.jid:
            self.botao_descer.hide()
            return
        barra = self.conversa.verticalScrollBar()
        no_fim = valor >= barra.maximum() - 40
        if no_fim:
            self._badge_naolidas = 0
            self._ix_ultima_naolida = None
            self.botao_descer.hide()
            return
        ix = self._ix_ultima_naolida
        if ix is not None and 0 <= ix < self.msgs.rowCount():
            vr = self.conversa.visualRect(self.msgs.index(ix))
            if vr.isValid() and vr.bottom() < 0:  # já passou para cima da tela
                self._badge_naolidas = 0
                self._ix_ultima_naolida = None
        self._mostrar_botao_descer()

    def _descer_rapido(self):
        self._badge_naolidas = 0
        self._ix_ultima_naolida = None
        self._ancorado = True
        self.botao_descer.hide()
        self._descer()

    def _picos_do_audio(self, msg):
        """Onda real em thread: decodificar opus no paint travaria a UI.

        Só roda quando o payload NÃO trouxe a onda — 99% dos áudios do acervo
        trazem `audioMessage.waveform`, e nesses o ffmpeg seria puro desperdício
        (e só apareceria depois do download).
        """
        if msg.get("_picos") is not None or not msg.get("_arquivo"):
            return
        if ((msg.get("message") or {}).get("audioMessage") or {}).get("waveform"):
            return

        def trabalho():
            picos = api.picos_de_audio(msg.get("_arquivo") or "")
            self._picos_prontos.emit(msg, picos)

        MIDIA.submit(trabalho)

    def _pintar_picos(self, msg, picos):
        msg["_picos"] = picos
        self.msgs.mudou(msg)  # repinta a onda com os picos reais

    def _repintar_gif(self, mid: str):
        """Quadro novo do GIF: repinta SÓ aquela linha.

        `viewport().update(rect)` no lugar de `dataChanged`: o segundo faria o
        QListView remedir o item 15x por segundo à toa — a altura da bolha não
        muda de um quadro para o outro.
        """
        for i, m in enumerate(self.msgs.itens):
            if (m.get("key") or {}).get("id") == mid:
                self.conversa.viewport().update(
                    self.conversa.visualRect(self.msgs.index(i))
                )
                return

    def _pintar_midia(self, msg, caminho):
        msg["_arquivo"] = caminho
        if (msg.get("messageType") or "") == "audioMessage":
            self._picos_do_audio(msg)  # onda real decodificada, sem travar o paint
        dono = (msg.get("key") or {}).get("remoteJid", "")
        if getattr(self, "_midias_alvo", 0):
            self._midias_feitas += 1
            if self._midias_feitas >= self._midias_alvo:
                self._midias_alvo = 0
                self._pintar_progresso(dono, -1, 1, "")
            else:
                self._pintar_progresso(
                    dono, self._midias_feitas, self._midias_alvo, "mídias"
                )
        QTimer.singleShot(0, self._descer)
        for i, m in enumerate(self.msgs.itens):
            if m is msg:
                ix = self.msgs.index(i)
                self.msgs.dataChanged.emit(ix, ix)
                return

    def menu_da_mensagem(self, ponto: QPoint):
        ix = self.conversa.indexAt(ponto)
        if not ix.isValid():
            return
        msg = ix.data(Qt.UserRole) or {}
        self.menu_para_mensagem(msg).exec(
            self.conversa.viewport().mapToGlobal(ponto)
        )

    def menu_para_mensagem(self, msg: dict, *, fechar=None) -> QMenu:
        """Fonte unica do menu da mensagem: a conversa e o visualizador usam
        este mesmo. Dois menus separados foi exatamente o que deu errado — o
        visualizador nasceu sem acao nenhuma e ninguem notou, porque a lista
        que a gente conferia era a do planejamento, nao a do WhatsApp Web.

        `fechar` e' o que o visualizador passa: as acoes que precisam da
        conversa a' vista (responder, encaminhar, ver na conversa) fecham o
        lightbox antes, como no Web.
        """
        meu = (msg.get("key") or {}).get("fromMe", False)
        menu = QMenu(self)

        def agir(fn, sai=False):
            """Envolve a acao: fecha o visualizador antes quando precisa."""
            def correr(*_):
                if sai and fechar:
                    fechar()
                fn()
            return correr

        reagir = menu.addMenu("😊  Reagir")
        for emoji in REACOES:
            reagir.addAction(emoji).triggered.connect(
                lambda _=False, e=emoji, m=msg: self.reagir(m, e)
            )
        reagir.addSeparator()
        reagir.addAction("Remover reação").triggered.connect(
            lambda _=False, m=msg: self.reagir(m, "")
        )

        mid = (msg.get("key") or {}).get("id", "")
        menu.addAction(
            "★  Remover dos favoritos" if mid in self.favoritas else "☆  Favoritar"
        ).triggered.connect(agir(lambda: self.favoritar(msg)))
        menu.addAction("↩  Responder").triggered.connect(
            agir(lambda: self.responder(msg), sai=True)
        )
        # No Web, "Copiar" numa imagem poe a IMAGEM na area de transferencia,
        # nao o rotulo dela. Copiar "🖼 legenda" e' o resultado inutil.
        arq = msg.get("_arquivo") or ""
        if arq and pathlib.Path(arq).suffix.lower() in IMAGEM_EXT:
            menu.addAction("⧉  Copiar imagem").triggered.connect(
                agir(lambda: QApplication.clipboard().setPixmap(QPixmap(arq)))
            )
        if texto := api.text_of(msg):
            menu.addAction("⧉  Copiar texto").triggered.connect(
                agir(lambda: QApplication.clipboard().setText(texto))
            )
        menu.addAction("➦  Encaminhar").triggered.connect(
            agir(lambda: self.encaminhar(msg), sai=True)
        )
        if arq:
            menu.addAction("⤓  Salvar arquivo").triggered.connect(
                agir(lambda: self.salvar_arquivo(msg))
            )
        if meu and msg.get("messageType") == "conversation":
            menu.addAction("✎  Editar").triggered.connect(
                agir(lambda: self.editar(msg), sai=True)
            )
        if fechar:  # so' faz sentido de dentro do visualizador
            menu.addAction("👁  Ver na conversa").triggered.connect(
                agir(lambda: self.ir_para_mensagem(msg), sai=True)
            )

        apagar = menu.addMenu("🗑  Apagar")
        apagar.addAction("Apagar para mim").triggered.connect(
            agir(lambda: self.apagar(msg, para_todos=False), sai=True)
        )
        if meu:
            apagar.addAction("Apagar para todos").triggered.connect(
                agir(lambda: self.apagar(msg, para_todos=True), sai=True)
            )
        return menu

    def ir_para_mensagem(self, msg: dict):
        """Rola ate a mensagem e a seleciona — o "Ver na conversa" do Web."""
        try:
            linha = self.msgs.itens.index(msg)
        except ValueError:
            return
        ix = self.msgs.index(linha)
        self._ancorado = False
        self.conversa.scrollTo(ix, QListView.PositionAtCenter)
        self._realcar(msg)

    def ir_para_mensagem_id(self, mid: str):
        """Idem, achando pelo ID — é assim que a citação sabe o alvo
        (`contextInfo.stanzaId`), já que ela guarda o id, não o objeto."""
        alvo = next((m for m in self.msgs.itens
                     if (m.get("key") or {}).get("id") == mid), None)
        if alvo is None:
            self.statusBar().showMessage(
                "a mensagem citada está fora do trecho carregado — role para cima"
            )
            return
        self.ir_para_mensagem(alvo)

    def _realcar(self, msg: dict):
        """Pisca a bolha por instantes, como o Web faz ao pular para a original."""
        msg["_realce"] = True
        self.msgs.mudou(msg)

        def apagar():
            if msg.pop("_realce", None):
                self.msgs.mudou(msg)

        QTimer.singleShot(1200, apagar)

    def favoritar(self, msg: dict):
        mid = (msg.get("key") or {}).get("id", "")
        if not mid:
            return
        if mid in self.favoritas:
            del self.favoritas[mid]
            msg.pop("_favorita", None)
            self.statusBar().showMessage("removida dos favoritos")
        else:
            guardado = {k: v for k, v in msg.items() if not k.startswith("_")}
            guardado["_chat"] = self.titulo.text()
            self.favoritas[mid] = guardado
            msg["_favorita"] = True
            self.statusBar().showMessage("★ favoritada")
        api.salvar_favoritas(self.favoritas)
        self.msgs.mudou(msg)

    def abrir_favoritas(self):
        if not self.favoritas:
            QMessageBox.information(self, "Favoritas", "Nenhuma mensagem favoritada ainda.")
            return
        linhas = []
        for m in list(self.favoritas.values())[-60:]:
            quando = datetime.fromtimestamp(m.get("messageTimestamp", 0)).strftime("%d/%m/%Y %H:%M")
            linhas.append(f"[{quando}] {m.get('_chat', '')}: {api.text_of(m)[:70]}")
        QMessageBox.information(
            self, f"★ {len(self.favoritas)} mensagens favoritas", "\n".join(reversed(linhas))
        )

    def responder(self, msg: dict):
        self.citando = msg
        autor = "Você" if (msg.get("key") or {}).get("fromMe") else (
            (msg.get("pushName") or "").strip() or self.titulo.text()
        )
        self.faixa_resposta.setText(f"↩  {autor}: {api.text_of(msg)[:70]}")
        self.caixa_resposta.setVisible(True)
        self.campo.setFocus()

    def cancelar_resposta(self):
        self.citando = None
        self.caixa_resposta.setVisible(False)

    def editar(self, msg: dict):
        atual = api.text_of(msg)
        novo, ok = QInputDialog.getMultiLineText(self, "Editar mensagem", "Texto:", atual)
        if not ok or not novo.strip() or novo.strip() == atual:
            return
        chave = msg.get("key") or {}

        def feito():
            msg["message"] = {"conversation": novo.strip()}
            msg["_editada"] = True
            self.msgs.mudou(msg)

        POOL.submit(self._depois, feito, self.api.editar, self.msgs.jid, chave, novo.strip())

    def apagar(self, msg: dict, para_todos: bool):
        alvo = api.text_of(msg)[:60] or "esta mídia"
        aviso = "Apagar para todos?" if para_todos else "Apagar só para você?"
        if QMessageBox.question(self, "Apagar", f"{aviso}\n\n{alvo}") != QMessageBox.Yes:
            return
        chave = msg.get("key") or {}
        if para_todos:
            def marcar():
                msg.setdefault("key", {})["deleted"] = True
                msg.pop("_arquivo", None)
                self.msgs.mudou(msg)

            POOL.submit(self._depois, marcar, self.api.apagar, chave)
        else:
            self.ocultos.add(chave.get("id", ""))
            api.salvar_ocultos(self.ocultos)
            self.msgs.remover(msg)

    def encaminhar(self, msg: dict):
        nomes = [api.nome_do_chat(c, self.chats.contatos) for c in self._todos[:200]]
        alvo, ok = QInputDialog.getItem(self, "Encaminhar para", "Conversa:", nomes, 0, False)
        if not ok:
            return
        destino = self._todos[nomes.index(alvo)]
        jid = destino["remoteJid"]
        if arq := msg.get("_arquivo"):
            POOL.submit(self._tentar, self.api.send_file, jid, pathlib.Path(arq), api.text_of(msg))
        else:
            POOL.submit(self._tentar, self.api.send_text, jid, api.text_of(msg))
        self.statusBar().showMessage(f"encaminhado para {alvo}")

    def salvar_arquivo(self, msg: dict):
        origem = pathlib.Path(msg.get("_arquivo", ""))
        if not origem.exists():
            return
        destino, _ = QFileDialog.getSaveFileName(self, "Salvar como", origem.name)
        if destino:
            shutil.copy2(origem, destino)

    def _depois(self, aoterminar, fn, *args):
        """Roda a chamada de rede e, se der certo, atualiza a UI na thread da GUI."""
        try:
            fn(*args)
            self._na_gui.emit(aoterminar)
        except Exception as e:
            log.error("falhou: %s", e)
            self._aviso.emit(f"falhou: {e}")

    def eventFilter(self, obj, ev):
        """Clique no avatar/nome do header abre Dados do contato (spec 2.0:
        zonas pontuais, não a barra inteira); dentro da bolha de áudio o clique
        é botão toca/pausa ou busca na onda."""
        if ev.type() == QEvent.MouseButtonPress and obj in (
            self.avatar_topo, self.zona_nome
        ):
            if self._chat_atual():
                self.abrir_dados_do_contato()
                return True
        if obj is self.lista.viewport() and ev.type() == QEvent.MouseButtonPress:
            # ponytail: `clicked` so' resolve o indice no release, e a reordenacao
            # agrupada pode cair no meio — o indice passaria a apontar para outra
            # conversa. Guardamos QUEM foi clicado, nao ONDE.
            ix = self.lista.indexAt(ev.position().toPoint())
            self._jid_clicado = (
                (ix.data(Qt.UserRole) or {}).get("remoteJid") if ix.isValid() else None
            )
        if obj is self.conversa.viewport() and ev.type() == QEvent.MouseButtonPress:
            ponto = ev.position().toPoint()
            ix = self.conversa.indexAt(ponto)
            if ix.isValid():
                msg = ix.data(Qt.UserRole) or {}
                if not api.foi_apagada(msg) and self._clique_local(ix, ponto, msg):
                    return True
                if not api.foi_apagada(msg) and self._clique_seta(ix, ponto, msg):
                    return True
                if not api.foi_apagada(msg) and self._clique_citacao(ix, ponto, msg):
                    return True
                if msg.get("messageType") == "audioMessage" and not api.foi_apagada(msg):
                    if self._clique_audio(ix, ponto, msg):
                        return True
                # por ultimo: so' a midia abre o visualizador, e so' se o clique
                # cair EM CIMA dela. Fora disso o clique morre aqui, como no Web.
                if not api.foi_apagada(msg) and self._clique_midia(ix, ponto, msg):
                    return True
        if obj is self.conversa.viewport() and ev.type() == QEvent.Resize:
            if getattr(self, "botao_descer", None) and self.botao_descer.isVisible():
                self._mostrar_botao_descer()
        return super().eventFilter(obj, ev)

    def _clique_local(self, ix, ponto, msg) -> bool:
        """Cartão de localização clicável: abre o ponto no Google Maps (sem
        tile server próprio, o mapa real fica no navegador — como o Web faz
        com 'Ver no mapa')."""
        if (msg.get("messageType") or "") != "locationMessage":
            return False
        opt = QStyleOptionViewItem()
        opt.rect = self.conversa.visualRect(ix)
        opt.font = self.conversa.font()
        delegado = self.conversa.itemDelegate()
        *_, larg, alto, _mt = delegado._medir(opt, ix)
        caixa, y = delegado.geometria_do_item(opt, ix, msg, larg, alto)
        if delegado._area_local(caixa, y).contains(ponto):
            det = api.detalhes(msg)
            lat, lon = det.get("lat"), det.get("lon")
            if lat is not None and lon is not None:
                QDesktopServices.openUrl(
                    QUrl(f"https://maps.google.com/?q={lat},{lon}")
                )
            return True
        return False

    def _clique_seta(self, ix, ponto, msg) -> bool:
        """Clique na ➦ de hover encaminha — o diálogo abre fora do event filter
        (modal dentro de eventFilter não pode)."""
        opt = QStyleOptionViewItem()
        opt.rect = self.conversa.visualRect(ix)
        opt.font = self.conversa.font()
        delegado = self.conversa.itemDelegate()
        *_, larg, alto, _mt = delegado._medir(opt, ix)
        meu = bool((msg.get("key") or {}).get("fromMe", False))
        caixa, _y = delegado.geometria_do_item(opt, ix, msg, larg, alto)
        if delegado._area_seta(caixa, meu).contains(ponto):
            QTimer.singleShot(0, lambda: self.encaminhar(msg))
            return True
        return False

    def _geometria(self, ix, msg):
        """(delegado, caixa, y, pix, det, larg) — a mesma conta do paint."""
        opt = QStyleOptionViewItem()
        opt.rect = self.conversa.visualRect(ix)
        opt.font = self.conversa.font()
        delegado = self.conversa.itemDelegate()
        _m, _t, pix, det, larg, alto, _mt = delegado._medir(opt, ix)
        caixa, y = delegado.geometria_do_item(opt, ix, msg, larg, alto)
        return delegado, caixa, y, pix, det

    def _clique_citacao(self, ix, ponto, msg) -> bool:
        """Clicar na citação pula para a mensagem original e a destaca — é o
        que o WhatsApp Web faz desde sempre."""
        alvo = id_citado(msg)
        if not alvo:
            return False
        delegado, caixa, y, _pix, _det = self._geometria(ix, msg)
        if not delegado._area_citacao(caixa, y).contains(ponto):
            return False
        self.ir_para_mensagem_id(alvo)
        return True

    def _clique_midia(self, ix, ponto, msg) -> bool:
        """Só a imagem/vídeo abre o visualizador. Clique na bolha ao lado, ou
        no fundo da conversa, não faz nada — o item do QListView ocupa a
        largura toda do viewport, então sem hit-test o vazio abria a mídia."""
        if not msg.get("_arquivo"):
            return False
        delegado, caixa, y, pix, _det = self._geometria(ix, msg)
        if pix is None or pix.isNull():
            return False
        if not delegado._area_midia(caixa, y, pix).contains(ponto):
            return False
        # modal nao pode abrir de dentro do eventFilter
        QTimer.singleShot(0, lambda: self.abrir_anexo(msg))
        return True

    def _clique_audio(self, ix, ponto, msg) -> bool:
        arquivo = msg.get("_arquivo")
        if not arquivo:
            self._buscar_midia(msg)
            self.statusBar().showMessage("baixando áudio…")
            return True
        meu = bool((msg.get("key") or {}).get("fromMe", False))
        # a caixa e o y vêm da MESMA replicação do paint — antes o hit-test
        # usava margem fixa e errava o alvo em janela larga e em grupo recuado
        delegado, caixa, y, _pix, _det = self._geometria(ix, msg)
        botao, onda, _retrato = delegado.geometria_audio(caixa, y)
        mid = (msg.get("key") or {}).get("id", "")
        quem = "Você" if meu else (msg.get("pushName") or self.titulo.text())
        if botao.contains(ponto):
            AUDIO.alternar(mid, arquivo, quem)
            return True
        if onda.contains(ponto):
            if AUDIO.mid != mid:
                AUDIO.alternar(mid, arquivo, quem)
            AUDIO.buscar(mid, (ponto.x() - onda.left()) / max(1, onda.width()))
            return True
        return False

    def abrir_anexo(self, msg: dict):
        """Abre no visualizador embutido, com as outras mídias do chat ao lado."""
        if not msg.get("_arquivo"):
            return
        midias = [m for m in self.msgs.itens if m.get("_arquivo")]
        try:
            posicao = midias.index(msg)
        except ValueError:
            return
        visor = Visualizador(midias, posicao, self.titulo.text(), self,
                             menu_de=self.menu_para_mensagem)
        visor.reagiu.connect(self.reagir)
        visor.exec()

    def reagir(self, msg: dict, emoji: str):
        chave = msg.get("key") or {}
        if not chave.get("id"):
            return
        POOL.submit(self._tentar, self.api.send_reaction, chave, emoji)
        self.statusBar().showMessage(f"reagiu {emoji}")

    # ---------- envio ----------

    def enviar_texto(self, texto):
        if not (jid := self.msgs.jid) or self.campo.isReadOnly():
            return
        self._desarquivar_jid(jid)  # Web: mandar mensagem tira a conversa do arquivo
        citada = self.citando
        citado = (citada or {}).get("key")
        # ponytail: a bolha aparece na hora com 🕐, como no WhatsApp Web. O
        # evento do servidor depois so' troca o status — `chegou` nao duplica
        # porque o id e' o mesmo.
        provisoria = self._eco_local(jid, texto, citada)
        POOL.submit(self._enviar_texto, jid, texto, citado, provisoria)
        self.cancelar_resposta()

    def _eco_local(self, jid: str, texto: str, citada: dict | None = None) -> dict:
        msg = {
            "key": {"id": f"local-{datetime.now().timestamp()}", "fromMe": True,
                    "remoteJid": jid},
            "message": {"conversation": texto},
            "messageType": "conversation",
            "messageTimestamp": int(datetime.now().timestamp()),
            "status": "PENDING",
        }
        if citada:
            # sem isto a citação só aparece depois de recarregar a conversa
            msg["contextInfo"] = {
                "quotedMessage": citada.get("message") or {},
                "participant": (citada.get("key") or {}).get("participant")
                or ("eu" if (citada.get("key") or {}).get("fromMe") else jid),
                "stanzaId": (citada.get("key") or {}).get("id"),
                "quotedPushName": citada.get("pushName"),
            }
        self._pendentes.append(msg)
        self.msgs.chegou(msg)
        self._ancorado = True
        self._descer()
        return msg

    def _enviar_texto(self, jid, texto, citado, provisoria):
        try:
            resposta = self.api.send_text(jid, texto, citado)
        except Exception as e:
            log.error("envio falhou: %s", e)

            def marcar_erro():
                provisoria["status"] = "ERROR"
                self.msgs.mudou(provisoria)

            self._na_gui.emit(marcar_erro)
            self._aviso.emit(f"não enviou: {e}")
            return

        def confirmar():
            # assume a chave de verdade: o evento do socket vira atualizacao,
            # nao uma segunda bolha
            if provisoria in self._pendentes:
                self._pendentes.remove(provisoria)
            provisoria["key"] = resposta.get("key") or provisoria["key"]
            provisoria["status"] = resposta.get("status") or "SERVER_ACK"
            self.msgs.mudou(provisoria)
            self.registrar_atividade(jid, provisoria)

        self._na_gui.emit(confirmar)

    def enviar_sticker(self):
        if not (jid := self.msgs.jid):
            return
        caminho, _ = QFileDialog.getOpenFileName(
            self, "Figurinha (webp, png)", "", "Imagens (*.webp *.png *.jpg *.jpeg)"
        )
        if caminho:
            POOL.submit(self._tentar, self.api.send_sticker, jid, pathlib.Path(caminho))

    def enviar_local(self):
        if not (jid := self.msgs.jid):
            return
        texto, ok = QInputDialog.getText(
            self, "Enviar localização", "latitude, longitude [, nome]:", text="-8.0476, -34.8770"
        )
        if not ok:
            return
        partes = [x.strip() for x in texto.split(",")]
        try:
            lat, lon = float(partes[0]), float(partes[1])
        except (ValueError, IndexError):
            self.statusBar().showMessage("formato: latitude, longitude")
            return
        nome = partes[2] if len(partes) > 2 else ""
        POOL.submit(self._tentar, self.api.send_location, jid, lat, lon, nome)

    def enviar_contato(self):
        if not (jid := self.msgs.jid):
            return
        texto, ok = QInputDialog.getText(self, "Enviar contato", "nome, telefone:")
        if not ok or "," not in texto:
            return
        nome, telefone = (x.strip() for x in texto.split(",", 1))
        POOL.submit(self._tentar, self.api.send_contact, jid, nome, telefone)

    def editar_ultima_enviada(self):
        """↑ no campo vazio: abre a edição da última mensagem que EU mandei.

        O WhatsApp só deixa editar por 15 minutos; passado isso a própria API
        recusa, então o aviso sai aqui em vez de abrir um diálogo inútil.
        """
        alvo = next(
            (m for m in reversed(self.msgs.itens)
             if (m.get("key") or {}).get("fromMe") and not api.foi_apagada(m)
             and (m.get("messageType") or "") in ("conversation", "extendedTextMessage")),
            None,
        )
        if alvo is None:
            return
        idade = datetime.now().timestamp() - alvo.get("messageTimestamp", 0)
        if idade > 15 * 60:
            self.statusBar().showMessage(
                "passou dos 15 minutos — o WhatsApp não deixa mais editar"
            )
            return
        self.editar(alvo)

    def silenciar_atual(self):
        if chat := self._chat_atual():
            self.alternar_mudo(chat.get("remoteJid", ""))

    def nova_conversa(self):
        """Ctrl+M — conversar com um número que não está na lista."""
        dlg = NovaConversa(self)
        if dlg.exec() != QDialog.Accepted or not dlg.destino:
            return
        jid = dlg.destino
        for i, c in enumerate(self.chats.itens):
            if jid in (c.get("_jids") or [c.get("remoteJid")]):
                self.lista.setCurrentIndex(self.chats.index(i))
                self.abrir_chat(self.chats.index(i))
                return
        # conversa que ainda não existe: entra na lista e abre vazia
        self.registrar_atividade(jid, {
            "key": {"id": f"novo-{datetime.now().timestamp()}", "fromMe": True,
                    "remoteJid": jid},
            "message": {"conversation": ""}, "messageType": "conversation",
            "messageTimestamp": int(datetime.now().timestamp()),
        })
        self._reordenar_agora()
        for i, c in enumerate(self.chats.itens):
            if c.get("remoteJid") == jid:
                self.lista.setCurrentIndex(self.chats.index(i))
                self.abrir_chat(self.chats.index(i))
                break

    def abrir_dados_do_contato(self):
        if chat := self._chat_atual():
            DadosDoContato(self, chat).exec()

    def _info_do_grupo(self, jid: str):
        """Subtítulo com os participantes e trava de "somente admins"."""
        info = self.api.grupo_info(jid)
        if not info:
            return
        resumo = self.api.resumo_participantes(info, self.chats.contatos)
        travado = bool(info.get("announce")) and not self.api.sou_admin(info)

        def pintar():
            if jid not in (self.msgs.jids or [self.msgs.jid]):
                return  # trocou de conversa enquanto a rede respondia
            self._grupo_resumo = resumo
            self._grupo_travado = travado
            self.travar_composer(
                "Somente admins podem enviar mensagens" if travado else ""
            )
            self.atualizar_subtitulo()

        self._na_gui.emit(pintar)

    def travar_composer(self, motivo: str):
        """Caixa fechada, como no WhatsApp quando o grupo é só de anúncios."""
        travado = bool(motivo)
        self.campo.setReadOnly(travado)
        self.campo.setPlaceholderText(
            motivo if travado else "Escreva uma mensagem — Ctrl+V cola arquivo"
        )
        for w in (self.b_mic, self.b_cam, self.b_emoji, self.b_gif):
            w.setEnabled(not travado)
        self.campo.setStyleSheet(
            f"background:{P['painel']}; color:{P['fraco']}; border:1px solid {P['divisor']};"
            if travado else ""
        )

    def abrir_busca_na_conversa(self):
        if not self.msgs.jid:
            return
        self.caixa_busca_chat.setVisible(True)
        self.busca_chat.setFocus()
        self.busca_chat.selectAll()
        self._btn_busca_ativa(True)

    def fechar_busca_na_conversa(self):
        self.caixa_busca_chat.setVisible(False)
        self.busca_chat.clear()
        self._achados = []
        self.conversa.viewport().update()
        self._btn_busca_ativa(False)

    def _btn_busca_ativa(self, ativa: bool):
        """A lupa fica destacada enquanto a busca na conversa está aberta."""
        self.btn_busca.setProperty("active", ativa)
        self.btn_busca.style().unpolish(self.btn_busca)
        self.btn_busca.style().polish(self.btn_busca)

    def buscar_na_conversa(self, termo: str):
        """Guarda o ID da mensagem, nunca a posicao dela.

        A conversa mexe embaixo da busca: com o teto de 500 atingido, cada
        mensagem nova faz _podar(do_fim=False) apagar do INICIO e deslocar
        tudo; "apagar para mim" desloca igual. Guardando indice cru, o ↑↓
        passava a destacar a mensagem vizinha — a que nao tem o termo — e
        podia apontar para fora da lista.
        """
        termo = termo.strip().lower()
        self._achados = (
            [mid for m in self.msgs.itens
             if termo in api.text_of(m).lower()
             and (mid := (m.get("key") or {}).get("id"))]
            if len(termo) >= 2
            else []
        )
        self._achado_atual = len(self._achados) - 1  # começa no mais recente
        self.contador_achados.setText(
            f"{self._achado_atual + 1}/{len(self._achados)}" if self._achados else
            ("nenhum" if len(termo) >= 2 else "")
        )
        if self._achados:
            self._ir_ao_achado()

    def navegar_achado(self, passo: int):
        if not self._achados:
            return
        self._achado_atual = (self._achado_atual + passo) % len(self._achados)
        self.contador_achados.setText(f"{self._achado_atual + 1}/{len(self._achados)}")
        self._ir_ao_achado()

    def _ir_ao_achado(self):
        """Resolve o ID para a linha AGORA — a lista pode ter mudado desde a busca."""
        if not self._achados:
            return
        alvo = self._achados[self._achado_atual]
        linha = next(
            (i for i, m in enumerate(self.msgs.itens)
             if (m.get("key") or {}).get("id") == alvo),
            None,
        )
        if linha is None:
            # saiu da janela viva (podada ou apagada): tira da lista e reconta
            self._achados.remove(alvo)
            if not self._achados:
                self.contador_achados.setText("nenhum")
                return
            self._achado_atual %= len(self._achados)
            self.contador_achados.setText(
                f"{self._achado_atual + 1}/{len(self._achados)}"
            )
            return self._ir_ao_achado()
        ix = self.msgs.index(linha)
        self._ancorado = False
        self.conversa.scrollTo(ix, QListView.PositionAtCenter)
        self._realcar(self.msgs.itens[linha])

    def info_do_chat(self):
        chat = self._chat_atual()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if jid.endswith("@g.us"):
            # ponytail: httpx sincrono com timeout de 60s na thread da GUI
            # congela a janela inteira. Vai para o pool, como todo o resto.
            self.statusBar().showMessage("carregando informações do grupo…")

            def trabalho():
                info = self.api.grupo_info(jid)
                partes = info.get("participants") or []
                linhas = [f"Grupo: {info.get('subject', '—')}",
                          f"Participantes: {len(partes)}", ""]
                for x in partes[:40]:
                    marca = " (admin)" if x.get("admin") else ""
                    numero = str(x.get("phoneNumber") or x.get("id", ""))
                    linhas.append(f"  {api.formatar_numero(numero)}{marca}")
                texto = "\n".join(linhas)
                self._na_gui.emit(
                    lambda: QMessageBox.information(self, "Informações", texto)
                )

            POOL.submit(trabalho)
            return

        linhas = [
            f"Nome: {api.nome_do_chat(chat, self.chats.contatos)}",
            f"JID: {jid}",
            f"Outros JIDs: {', '.join(chat.get('_jids', []))}",
        ]
        QMessageBox.information(self, "Informações", "\n".join(linhas))

    @property
    def gravador(self) -> Gravador:
        if self._gravador is None:
            self._gravador = Gravador(self)
            self._gravador.pronto.connect(self._ptt_pronto)
        return self._gravador

    def alternar_ptt(self):
        if not self.msgs.jid:
            return
        if self.gravador.gravando():
            self.gravador.parar()
            self.b_mic.setText("🎤")
            self.pulso_ptt.stop()
            self.statusBar().showMessage("enviando áudio…")
            return
        self.gravador.iniciar()
        self.b_mic.setText("⏹")
        self.pulso_ptt.start(500)
        POOL.submit(self.api.presenca, self.msgs.jid, "recording", 60_000)

    def _ptt_pronto(self, arquivo: pathlib.Path):
        if jid := self.msgs.jid:
            self._desarquivar_jid(jid)  # Web: mandar mensagem tira do arquivo
            POOL.submit(self._tentar, self.api.send_audio, jid, arquivo)
            self.statusBar().showMessage("áudio enviado")

    def abrir_camera(self):
        if not self.msgs.jid:
            return
        dlg = DialogoCamera(self)
        if dlg.exec() != QDialog.Accepted or not dlg.resultado:
            return
        jid = self.msgs.jid
        if dlg.como_recado:
            POOL.submit(self._tentar, self.api.send_video_nota, jid, dlg.resultado)
            self.statusBar().showMessage("vídeo-recado enviado")
        else:
            self.enviar_arquivos([dlg.resultado])

    def abrir_emoji(self):
        """Popup de emoji ancorado sobre o rodapé — o clique insere no cursor."""
        if not self.msgs.jid or self._pop_emoji is not None:
            return
        pop = SeletorEmoji(self)
        self._pop_emoji = pop
        pop.escolhido.connect(self.campo.insertPlainText)
        pop.finished.connect(lambda _r: setattr(self, "_pop_emoji", None))
        origem = self.campo.mapToGlobal(QPoint(0, 0))
        tela = QApplication.primaryScreen().availableGeometry()
        x = max(tela.left(), min(origem.x(), tela.right() - pop.width() + 1))
        pop.move(x, max(tela.top(), origem.y() - pop.height() - 4))
        pop.show()
        pop.busca.setFocus()

    def abrir_gif(self):
        """Popup de GIF ancorado sobre o rodapé — escolher baixa o mp4 e abre
        o diálogo de envio (sempre confirma antes de mandar)."""
        if not self.msgs.jid or self._pop_gif is not None:
            return
        pop = SeletorGif(self)
        # sem isto cada abertura deixava um dialogo pendurado na Janela (o pai
        # segura a referencia) — com QMovie dentro, seria CPU presa junto
        pop.setAttribute(Qt.WA_DeleteOnClose)
        self._pop_gif = pop
        pop.escolhido.connect(self._enviar_gif)
        pop.finished.connect(lambda _r: setattr(self, "_pop_gif", None))
        origem = self.campo.mapToGlobal(QPoint(0, 0))
        tela = QApplication.primaryScreen().availableGeometry()
        x = max(tela.left(), min(origem.x(), tela.right() - pop.width() + 1))
        pop.move(x, max(tela.top(), origem.y() - pop.height() - 4))
        pop.show()
        pop.busca.setFocus()

    def _enviar_gif(self, result: dict):
        """mp4 do Giphy baixado em thread; pronto, abre o diálogo de envio."""
        url = result.get("mp4") or ""
        if not url:
            return
        self.statusBar().showMessage("baixando GIF…")

        def trabalho():
            caminho = api.gif_baixar(url)
            self._gif_baixado.emit(str(caminho) if caminho else "")

        POOL.submit(trabalho)

    def _gif_baixado(self, caminho):
        if caminho:
            self.statusBar().showMessage("GIF pronto — confira o envio")
            # veio do seletor do Giphy: pede GIF de verdade, com loop
            self.enviar_arquivos([pathlib.Path(caminho)], gif=True)
        else:
            self.statusBar().showMessage("não consegui baixar o GIF — tente outro")

    def digitando(self):
        """'digitando…' do outro lado — no máximo um aviso a cada 3 s."""
        agora = datetime.now().timestamp()
        if not self.msgs.jid or agora - self._ultima_presenca < 3:
            return
        self._ultima_presenca = agora
        POOL.submit(self.api.presenca, self.msgs.jid, "composing", 3000)

    def escolher_arquivo(self):
        caminhos, _ = QFileDialog.getOpenFileNames(self, "Enviar arquivo")
        if caminhos:
            self.enviar_arquivos([pathlib.Path(c) for c in caminhos])

    def enviar_arquivos(self, caminhos, gif: bool = False):
        """Nunca envia direto: o WhatsApp Web sempre confirma antes, e colar um
        arquivo por engano não pode virar mensagem enviada."""
        if not (jid := self.msgs.jid) or not caminhos or self.campo.isReadOnly():
            return
        dlg = DialogoEnvio(caminhos, self.titulo.text(), self)
        if pre := self.campo.toPlainText().strip():
            dlg.legenda.setText(pre)
        if dlg.exec() != QDialog.Accepted:
            return
        finais, legenda = dlg.preparar()
        hd = dlg.hd.isChecked()
        self.campo.clear()
        citado = (self.citando or {}).get("key")
        self._desarquivar_jid(jid)  # Web: mandar mensagem tira do arquivo
        for c in finais:
            e_img = c.suffix.lower() in IMAGEM_EXT
            POOL.submit(self._enviar_media, jid, c, legenda, citado, hd and e_img, gif)
            legenda, citado = "", None  # so a primeira leva legenda e citacao
        self.cancelar_resposta()

    def _enviar_media(self, jid, caminho, legenda, citado, hd, gif: bool = False):
        """send_file + registro local do HD (a Evolution não marca no payload —
        sem o mapa, o selo sumiria ao recarregar a conversa).

        Roda no POOL: nada de Qt aqui. Desarquivar mexe no modelo da lista e
        por isso ficou no chamador, na thread da GUI — como em enviar_texto.
        """
        try:
            resp = self.api.send_file(jid, caminho, legenda, citado, gif=gif)
        except Exception as e:
            log.error("envio de mídia falhou: %s", e)
            self._aviso.emit(f"falhou: {e}")
            return
        if hd:
            mid = ((resp or {}).get("key") or {}).get("id")
            if mid:
                HD_IDS.add(mid)
                api.salvar_hd_ids(HD_IDS)

    def _tentar(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            log.error("envio falhou: %s", e)
            self._aviso.emit(f"falhou: {e}")

    # ---------- tempo real ----------

    def sincronizar_chats(self, itens: list):
        """Traz do WhatsApp o que a Evolution repassar: mute, arquivado, não lidas."""
        mudou = False
        for x in itens:
            if api.importar_mute(x, self.mudos):
                mudou = True
            jid = x.get("remoteJid") or x.get("id") or ""
            if (n := x.get("unreadCount")) is not None:
                for c in self._todos:
                    if jid in (c.get("_jids") or [c.get("remoteJid")]):
                        c["unreadCount"] = int(n)
                        self.chats.mudou(c)
                        break
        if mudou:
            api.salvar_silenciados(self.mudos)
            self._pintar_chats(self._todos)
            log.info("mute importado do WhatsApp para %d conversas", len(self.mudos))

    def _adotar_eco(self, vindo: dict) -> bool:
        """O socket pode entregar `send.message` antes de o POST retornar.

        Nesse instante a bolha local ainda tem id provisório, a deduplicação por
        id não casa e a mensagem apareceria duas vezes. Aqui a bolha pendente
        adota a chave que chegou e o evento não vira uma segunda linha.
        """
        texto = api.text_of(vindo)
        jid = (vindo.get("key") or {}).get("remoteJid", "")
        for eco in list(self._pendentes):
            if eco.get("_adotado"):
                continue
            if (eco.get("key") or {}).get("remoteJid") != jid:
                continue
            if api.text_of(eco) != texto:
                continue
            eco["key"] = vindo.get("key") or eco["key"]
            eco["status"] = vindo.get("status") or eco.get("status")
            eco["_adotado"] = True
            self._pendentes.remove(eco)
            self.msgs.mudou(eco)
            self.registrar_atividade(jid, eco)
            return True
        return False

    def registrar_atividade(self, jid: str, msg: dict):
        """Mensagem nova reordena a lista inteira, não só o que está visível.

        Vale para mensagem recebida e para mensagem enviada de outro aparelho —
        ambas chegam pelo socket (`messages.upsert` e `send.message`).
        """
        alvo = None
        for c in self._todos:
            if jid in (c.get("_jids") or [c.get("remoteJid")]):
                alvo = c
                break

        if alvo is None:  # conversa que ainda não existia na lista
            alvo = {
                "remoteJid": jid,
                "_jids": [jid],
                "pushName": (msg.get("pushName") or "").strip(),
                "unreadCount": 0,
                "_apelidos": self.apelidos,
            }
            self._todos.append(alvo)
            POOL.submit(self._buscar_foto, alvo)

        alvo["lastMessage"] = msg
        alvo["updatedAt"] = datetime.fromtimestamp(
            msg.get("messageTimestamp", 0) or datetime.now().timestamp()
        ).isoformat()
        aberta = jid in (self.msgs.jids or [self.msgs.jid])
        if not aberta and not (msg.get("key") or {}).get("fromMe"):
            alvo["unreadCount"] = int(alvo.get("unreadCount") or 0) + 1
        elif aberta:
            alvo["unreadCount"] = 0

        # ponytail: rajada de mensagens nao pode virar uma reordenacao por
        # mensagem — `aplicar_filtros` varre os 557 chats quatro vezes e reseta
        # o modelo. Agrupa em uma so' passada.
        self._pede_reordem()

    def _guardar_contatos(self, mapa: dict):
        self.chats.contatos = mapa
        CONTATOS.clear()
        CONTATOS.update(mapa)
        for c in self._todos:  # o nome do chat também resolve citação em 1:1
            nome = api.nome_do_chat(c, mapa)
            for j in c.get("_jids") or [c.get("remoteJid")]:
                CONTATOS.setdefault(j, nome)

    def _pintar_progresso(self, jid: str, feito: int, alvo: int, etapa: str):
        """Progresso de UMA conversa. Fica guardado por jid, então abrir outra
        conversa no meio não herda a barra — e voltar a mostra de novo."""
        if feito < 0:
            self._carregando.pop(jid, None)
            self.chats.carregando = self._carregando
        else:
            self._carregando[jid] = (feito, max(1, alvo), etapa)
        self.chats.carregando = self._carregando

        # a linha da lista pinta a própria barra no lugar da prévia
        for i, c in enumerate(self.chats.itens):
            if jid in (c.get("_jids") or [c.get("remoteJid")]):
                self.chats.mudou(c)
                break

        self._barra_da_conversa()

    def _barra_da_conversa(self):
        """A barra do topo só existe para a conversa que está aberta."""
        atual = self._carregando.get(self.msgs.jid)
        if not atual:
            self.carregando.setVisible(False)
            self.etapa.setVisible(False)
            return
        feito, alvo, _etapa = atual
        self.carregando.setMaximum(alvo)
        self.carregando.setValue(min(feito, alvo))
        self.carregando.setVisible(True)
        self.etapa.setText(f"{int(100 * min(feito, alvo) / alvo)}%")
        self.etapa.setVisible(True)

    def _limpar_cache(self):
        agora = datetime.now().timestamp()
        vencidas = [
            jid for jid, d in self._cache.items()
            if d["saiu_em"] and agora - d["saiu_em"] > api.VALIDADE_CACHE
        ]
        for jid in vencidas:
            del self._cache[jid]
        if vencidas:
            log.info("cache: %d conversas esquecidas · rss=%dMB", len(vencidas), rss_mb())

        limite = float(self.cfg.get("cache_gb") or 0.5)
        if api.cache_tamanho() > limite * 1024**3:
            POOL.submit(self._rotacionar_cache, limite)

    def _rotacionar_cache(self, limite_gb: float):
        liberado = api.cache_rotacionar(limite_gb)
        if liberado:
            self._aviso.emit(f"cache rotacionado: {liberado / 1024**2:.0f} MB liberados")
            log.info("cache rotacionado: %.0f MB", liberado / 1024**2)

    def _guardar_no_cache(self):
        """Chamado ao trocar de conversa: a anterior fica em espera."""
        self._limpar_cache()  # de graça: já estamos mexendo no cache
        if not self.msgs.jid:
            return
        self._cache[self.msgs.jid] = {
            "itens": self.msgs.itens,
            "jids": self.msgs.jids,
            "pagina": self.msgs.pagina,
            "paginas": self.msgs.paginas,
            "saiu_em": datetime.now().timestamp(),
        }

    def _pede_reordem(self):
        self._ordem_suja = True
        if not self._debounce_ordem.isActive():
            self._debounce_ordem.start(120)

    def _reordenar_agora(self):
        if not self._ordem_suja:
            return
        self._ordem_suja = False
        self._todos = api.ordenar(self._todos, self.fixados)
        self.aplicar_filtros()

    def mostrar_conexao(self, estado: str):
        """Queda do socket precisa ficar na tela, nao passar como recado.

        Enquanto o socket esta' fora nao chega mensagem nenhuma; sem aviso, a
        tela silenciosa e' idêntica a 'ninguem escreveu'.
        """
        caiu = estado in ("caiu", "offline", "close", "connecting")
        self.statusBar().showMessage(
            f"⚠ sem conexão ({estado}) — reconectando…" if caiu else f"conexão: {estado}"
        )

    def mostrar_presenca(self, jid: str, estado: str):
        """Presença no subtítulo, prioridade do Web: digitando/gravando somem
        em 6s; available vira 'online' persistente até unavailable/offline."""
        if jid not in (self.msgs.jids or [self.msgs.jid]):
            return
        efemero = {"composing": "digitando…", "recording": "gravando áudio…"}.get(estado)
        if efemero:
            self._presenca_efemera = (jid, datetime.now().timestamp() + 6, efemero)
            self.atualizar_subtitulo()
            QTimer.singleShot(6500, self.atualizar_subtitulo)
            return
        if estado == "available" and not jid.endswith("@g.us"):
            self._online_1a1 = True
        elif estado in ("unavailable", "offline"):
            self._online_1a1 = False
        self.atualizar_subtitulo()

    def atualizar_subtitulo(self):
        """Fonte única do subtítulo do header (spec 2.0).

        Ordem: digitando/gravando (efêmero 6s) > silenciada > fixada, em 1:1;
        em grupo: trava de admin > participantes. 'online' só em 1:1 e cai
        sozinho quando a presença vira unavailable/offline.
        """
        agora = datetime.now().timestamp()
        jids = self.msgs.jids or ([self.msgs.jid] if self.msgs.jid else [])
        jid = jids[0] if jids else ""
        cor = P["fraco"]
        efemero = None
        if self._presenca_efemera and self._presenca_efemera[0] == jid:
            _, ate, texto = self._presenca_efemera
            if agora < ate:
                efemero = texto
            else:
                self._presenca_efemera = None
        if efemero:
            texto, cor = efemero, P["accent"]
        elif jid.endswith("@g.us"):
            if self._grupo_travado:
                texto = "🔒 Somente admins podem enviar mensagens"
            else:
                texto = self._grupo_resumo  # "" enquanto a rede não respondeu
        elif api.esta_mudo(self.mudos, jid):
            texto = "🔕 Silenciada"
        elif jid in self.fixados:
            texto = "📌 Fixada"
        elif self._online_1a1:
            texto, cor = "online", P["accent"]
        else:
            texto = ""
        self.subtitulo.setText(texto)
        self.subtitulo.setStyleSheet(
            f"color:{cor}; font-size:{ESCALA.px() - 2}px; background:transparent;"
        )

    def mostrar_menu_conversa(self):
        """Menu ⋮ do header — ordem do WhatsApp Web BR filtrada pela API 2.3.7."""
        chat = self._chat_atual()
        if not chat:
            return
        jid = chat["remoteJid"]
        menu = QMenu(self)
        menu.addAction("ℹ  Dados do contato").triggered.connect(
            self.abrir_dados_do_contato
        )
        if api.esta_mudo(self.mudos, jid):
            menu.addAction("🔔  Ativar notificações").triggered.connect(
                lambda: self.silenciar(jid, None)
            )
        else:
            sub = menu.addMenu("🔕  Silenciar notificações")
            for rotulo, segundos in api.DURACOES_MUDO:
                sub.addAction(rotulo).triggered.connect(
                    lambda _=False, s=segundos: self.silenciar(jid, s)
                )
        menu.addAction(
            "📌  Desafixar conversa" if jid in self.fixados else "📌  Fixar conversa"
        ).triggered.connect(lambda: self.alternar_fixado(jid))
        menu.addSeparator()
        menu.addAction("✉  Marcar como não lida").triggered.connect(
            self.nao_lida_atual
        )
        menu.exec(self.btn_menu.mapToGlobal(QPoint(0, self.btn_menu.height())))

    def mostrar_menu_usuario(self):
        """Menu do avatar do usuário (faixa da sidebar) — casa da Config global."""
        menu = QMenu(self)
        menu.addAction("👤  Meu perfil").triggered.connect(self.abrir_meu_perfil)
        menu.addAction("🔒  Privacidade").triggered.connect(self.abrir_privacidade)
        menu.addSeparator()
        menu.addAction("⚙  Configurações").triggered.connect(self.abrir_config)
        menu.addSeparator()
        menu.addAction("Sair").triggered.connect(QApplication.instance().quit)
        menu.exec(
            self.avatar_usuario.mapToGlobal(QPoint(0, self.avatar_usuario.height()))
        )

    def abrir_meu_perfil(self):
        MeuPerfil(self).exec()
        POOL.submit(self._carregar_usuario)   # nome/foto podem ter mudado

    def abrir_privacidade(self):
        Privacidade(self).exec()

    def atualizar_status(self, mid: str, status: str, jid: str):
        if status == "DELETED":
            self.marcar_apagada({"key": {"id": mid}})
            return
        def avanca(alvo: dict) -> bool:
            """Só sobe na escala: DELIVERY_ACK depois de READ é ruído."""
            anterior = api.status_de(alvo)
            if status in api.PROGRESSO and anterior in api.PROGRESSO:
                if api.PROGRESSO.index(status) <= api.PROGRESSO.index(anterior):
                    return False
            alvo["status"] = status
            return True

        for m in self.msgs.itens:
            if (m.get("key") or {}).get("id") == mid:
                if avanca(m):
                    self.msgs.mudou(m)
                break

        # a barra lateral tem o SEU proprio objeto de lastMessage — atualizar a
        # conversa nao a alcanca. Sem isto a previa fica presa no 🕐 do envio.
        for c in self._todos:
            ultima = c.get("lastMessage") or {}
            if (ultima.get("key") or {}).get("id") == mid:
                if avanca(ultima):
                    self.chats.mudou(c)
                break

    def marcar_apagada(self, aviso: dict):
        alvo = (aviso.get("key") or {}).get("id")
        for m in self.msgs.itens:
            if (m.get("key") or {}).get("id") == alvo:
                m.setdefault("key", {})["deleted"] = True
                m.pop("_arquivo", None)
                self.msgs.mudou(m)
                return

    def mensagem_recebida(self, msg):
        jid = (msg.get("key") or {}).get("remoteJid", "")
        if par := api.reacao_de(msg):
            alvo, emoji, autor = par
            for m in self.msgs.itens:
                if (m.get("key") or {}).get("id") == alvo:
                    api.aplicar_reacao(m, autor, emoji)
                    self.msgs.mudou(m)
                    break
            return
        if not api.e_visivel(msg) or (msg.get("key") or {}).get("id") in self.ocultos:
            return
        if (msg.get("key") or {}).get("fromMe") and self._adotar_eco(msg):
            return
        if jid in (self.msgs.jids or [self.msgs.jid]):
            no_fim = self._ancorado or self.conversa.verticalScrollBar().value() >= (
                self.conversa.verticalScrollBar().maximum() - 40
            )
            self.msgs.chegou(msg)
            self._buscar_midia(msg)
            self._ancorado = no_fim
            self._descer()
            if not (msg.get("key") or {}).get("fromMe"):
                POOL.submit(self.api.marcar_lida, [msg["key"]])
                if not no_fim:
                    # mensagem nova abaixo do que está na tela: o ↓ reaparece
                    self._badge_naolidas = int(self._badge_naolidas or 0) + 1
                    self._mostrar_botao_descer()
        else:
            # ponytail: baixa a midia mesmo com o chat fechado. E' a unica forma
            # de ter acervo — o CDN do WhatsApp expira sem perguntar se voce olhou.
            self._buscar_midia(msg)

        self.registrar_atividade(jid, msg)
        if not (msg.get("key") or {}).get("fromMe") and jid != self.msgs.jid:
            if self.cfg.get("notificar") and not api.esta_mudo(self.mudos, jid):
                remetente = (msg.get("pushName") or "").strip() or api.formatar_numero(jid)
                notificar(remetente, api.text_of(msg))


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    cfg = api.load_config()
    app = QApplication(sys.argv)
    app.setApplicationName("zap-evo")
    app.setApplicationDisplayName("zap-evo")
    # no Wayland é isto que liga a janela ao .desktop — sem ele o compositor
    # mostra "main.py" e o ícone genérico
    app.setDesktopFileName("zap-evo")
    app.setWindowIcon(QIcon.fromTheme("zap-evo"))
    ESCALA.valor = max(Escala.MIN, min(Escala.MAX, int(cfg.get("escala") or Escala.BASE)))
    P.update(PALETAS.get(cfg.get("tema", "whatsapp escuro"), PALETAS["whatsapp escuro"]))
    app.setStyleSheet(tema_css())
    global ANIMA
    ANIMA = Animador()  # depende do QApplication: cria depois, nunca no import
    janela = Janela(cfg)
    janela.show()
    log.info("rss inicial=%dMB", rss_mb())
    codigo = app.exec()

    # Os dois pools sao de threads NAO-daemon e o cliente http tem timeout de
    # 60s: sem isto, fechar a janela com uma fila de avatares pendente deixava
    # o processo vivo esperando a fila inteira terminar. Cancelamos o que ainda
    # nao comecou e saimos — nao ha nada para descarregar, todo estado (config,
    # fixados, arquivadas...) e' gravado na hora em que muda.
    POOL.shutdown(wait=False, cancel_futures=True)
    MIDIA.shutdown(wait=False, cancel_futures=True)
    os._exit(codigo)


if __name__ == "__main__":
    main()
