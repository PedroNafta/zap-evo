# zap-evo

Cliente desktop nativo (Python + Qt/PySide6) para WhatsApp, conectado a uma
instância própria da [Evolution API](https://github.com/evolution-foundation/evolution-api).
Projeto pessoal, feito para substituir clientes baseados em webview e reduzir o
consumo de memória: ~360 MB em uso contra mais de 5 GB do cliente anterior
após um dia aberto.

> Usa uma API **não oficial** do WhatsApp (Baileys, via Evolution). Uso por
> conta e risco de quem roda.

## Recursos

- Lista de conversas, mensagens em tempo real (Socket.IO) e histórico sob demanda
- Envio de texto, mídia, áudio, documentos, figurinhas e GIFs
- Reações, respostas com citação, cartões de link e mensagens de voz com forma de onda
- Painel de mídia, documentos e links por conversa
- Busca global no histórico (opcional, via Postgres da Evolution em modo leitura)
- Temas claro/escuro

## Requisitos

- Linux com Python 3.11+ e [uv](https://docs.astral.sh/uv/)
- Uma instância da Evolution API (testado na 2.3.7) já pareada com o número

## Uso

```sh
uv run main.py
```

As dependências estão no cabeçalho PEP 723 do `main.py`; o `uv` resolve na
primeira execução. Não há build.

Na primeira abertura é criado `~/.config/evolution-client/config.toml`
(modo 600), editável pelo ⚙ do app:

```toml
host = "http://localhost:8080"
apikey = "sua-apikey"
instance = "minha-instancia"
me = "558199990000"
```

Campos opcionais: `pg_*` (busca global) e `giphy_apikey` (seletor de GIFs).

## GIF em loop (patch opcional na Evolution)

A Evolution 2.3.7 força `gifPlayback = false` em todo envio de vídeo, então o
GIF chega como vídeo mudo. Uma linha no `dist/main.js` da instância faz ela
respeitar o campo enviado pelo cliente:

```diff
- e.mediatype==="video"&&(i[n].gifPlayback=!1)
+ e.mediatype==="video"&&(i[n].gifPlayback=e.gifPlayback===!0)
```

Sem o campo, o comportamento continua idêntico ao original.

## Testes

```sh
uv run --with httpx python api.py               # autoteste da camada de API
QT_QPA_PLATFORM=offscreen uv run t_conversa.py  # idem para t_gif.py e t_painel.py
```

Desenvolvido com auxílio de IA (Claude Code).
