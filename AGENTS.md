# AGENTS.md

DVR caseiro: **um** container Docker que transmite uma webcam USB como HLS,
grava segmentos MP4 de 10 min com retenção de 24 h e expõe um Cloudflare Quick
Tunnel sob demanda. Alvo: Ubuntu 24.04, Celeron N3350 / 4 GB RAM. Porta **2310**.
A raiz do repositório **é** a raiz do projeto (sem subpasta `dvr-yoosee/`).

## Comandos

- Subir/rebuild: `docker compose up -d --build`
- Logs: `docker compose logs -f dvr` · Shell: `docker compose exec dvr bash`
- Validar o compose: `docker compose config`

Não há suite de testes, linter, formatador nem CI. Não invente comandos.

**Não dá para rodar isto na máquina Windows de desenvolvimento**: FFmpeg/cloudflared
e `/dev/video0` são só Linux. Verifique buildando a imagem e subindo no host Ubuntu
(`docker run --device /dev/video0 ...`). `python -m py_compile app/*.py` serve
apenas como checagem de sintaxe.

## Arquitetura (container único, de propósito)

- `app/main.py` — app FastAPI, middleware de Basic Auth, todas as rotas, e o
  `lifespan` que sobe/derruba os três gerentes. **Só um worker uvicorn** (o estado
  vive no processo — não aumente `--workers`).
- `app/streamer.py` — processo FFmpeg persistente `/dev/video0` -> `/app/hls`.
  Watchdog reinicia se cair.
- `app/recorder.py` — FFmpeg gera MP4 em `/app/recordings` + task de retenção.
- `app/tunnel.py` — subprocesso `cloudflared`, URL raspada do stderr, TTL 4 h.
- `app/config.py` — **único** lugar que lê variáveis de ambiente.
- `app/procutils.py` — encerramento compartilhado de subprocessos.
- `static/` — HTML/CSS/JS puros, sem build; `hls.js` vem de CDN.

## Pegadinhas que derrubam quem mexe

- **Uma câmera, um leitor.** Webcam USB (uvcvideo) só aceita UM processo por vez.
  Por isso o gravador NÃO abre `/dev/video0` por padrão: ele re-muxa o HLS ao vivo
  (`-c copy`) de `http://localhost:2310/hls/stream.m3u8`. Isso também evita um
  segundo encode x264 no hardware fraco. Só use `DVR_RECORD_SOURCE=device` se a
  câmera suportar múltiplas aberturas.
- **A porta 2310 aparece em 4 lugares** e precisa ficar em sincronia: `docker-compose.yml`,
  `uvicorn --port` no `Dockerfile`, `config.PORT` e o alvo do túnel.
- **`/hls/*` é público de propósito** (o player não envia Basic Auth). Todo o resto
  fica atrás do `auth_middleware` em `main.py`; `/healthz` também é aberto.
- **A retenção nunca apaga o segmento mais recente** (está sendo gravado) e calcula
  a idade pelo timestamp do **nome do arquivo**, não pela contagem. O regex
  `_NAME_RE` em `recorder.py` depende do `-strftime 1`; se mudar o padrão de nome,
  atualize o regex.
- **Parada graciosa difere:** o gravador recebe `q` no stdin (escreve o `moov` do
  MP4) — por isso ele usa `stdin=PIPE` e NÃO pode ganhar `-nostdin`. O streamer é
  morto com SIGTERM (HLS não precisa finalizar).
- **`recordings/` é bind mount no host** (persistente, grande). **`/app/hls` é
  tmpfs** (efêmero) — nunca coloque dado persistente lá.
- O container roda como **root** de propósito (permissões de device e de bind mount).
  `devices: [/dev/video0]` + `group_add: [video]` continuam no compose. **Não** adicione
  `privileged: true`.
- `DVR_PASSWORD` tem padrão `changeme` e o boot emite um aviso. Nunca faça commit de
  senha real: use `.env` (veja `.env.example`, já no `.gitignore`/`.dockerignore`).
- Imagem preta/ruim da webcam? Defina `DVR_INPUT_FORMAT=mjpeg` (a maioria das UVC).
- Alterações de código exigem rebuild: só `recordings/` e `hls/` são volumes; o código
  é copiado para dentro da imagem.

## Convenções

- Toda configuração é variável de ambiente, lida apenas em `app/config.py`.
- Comentários e logs em português — mantenha o estilo.
- Dependências mínimas: `requirements.txt` é só FastAPI + uvicorn.
