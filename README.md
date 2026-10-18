# DVR — Câmera USB com HLS, gravação 24 h e Quick Tunnel

Sistema de DVR caseiro para **uma webcam USB** (`/dev/video0`), em **um único
container** Docker:

- **Stream ao vivo** via HLS (funciona em qualquer navegador; player `hls.js`).
- **Gravação cíclica** em segmentos MP4 de 10 minutos com **retenção de 24 h**.
- **Cloudflare Quick Tunnel** sob demanda, com expiração automática em 4 h.
- **Autenticação HTTP Basic** em tudo, exceto o próprio stream HLS.

Feito para hardware modesto (Celeron N3350 / 4 GB RAM) e Ubuntu Server 24.04.
Porta principal: **2310**.

## Estrutura

```
.
├── docker-compose.yml     # orquestração (EasyPanel ou `docker compose up`)
├── Dockerfile             # Python 3.12 + FFmpeg + cloudflared
├── requirements.txt
├── app/
│   ├── main.py            # FastAPI: rotas, auth, ciclo de vida
│   ├── config.py          # TODAS as variáveis de ambiente
│   ├── streamer.py        # FFmpeg -> HLS ao vivo (+ watchdog)
│   ├── recorder.py        # FFmpeg -> segmentos MP4 + retenção 24 h
│   ├── tunnel.py          # subprocesso cloudflared (+ TTL de 4 h)
│   └── procutils.py       # encerramento de subprocessos
├── static/                # index.html, tunnel.html, style.css, app.js
├── recordings/            # (volume) gravacoes .mp4
└── hls/                   # (tmpfs) segmentos HLS ao vivo
```

## Como subir

### No host, com Docker Compose

```bash
cp .env.example .env      # edite DVR_PASSWORD (OBRIGATÓRIO)
docker compose up -d --build
```

Acesse `http://IP-DO-SERVIDOR:2310`. O navegador pedirá usuário e senha
(`DVR_USER` / `DVR_PASSWORD`).

### No EasyPanel

1. Crie um App do tipo **Compose** apontando para este repositório.
2. Defina as variáveis de ambiente `DVR_USER`, `DVR_PASSWORD` e `TZ`.
3. Publique. O EasyPanel cuida do build e do proxy; a porta interna é `2310`.

> **Pré-requisito de hardware:** `/dev/video0` deve existir no host. No host,
> confirme com `ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 teste.jpg`.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `DVR_USER` | `admin` | Usuário do HTTP Basic |
| `DVR_PASSWORD` | `changeme` | Senha do HTTP Basic (**troque!**) |
| `TZ` | `America/Sao_Paulo` | Fuso horário dos nomes/segmentos |
| `DVR_INPUT_FORMAT` | *(vazio)* | Formato de captura (`mjpeg` resolve imagem preta/ruim) |
| `DVR_VIDEO_SIZE` | `640x480` | Resolução |
| `DVR_FRAMERATE` | `15` | FPS |
| `DVR_VIDEO_BITRATE` | `1200k` | Bitrate do H.264 |
| `DVR_X264_TUNE` | `zerolatency` | Deixe vazio para melhor compressão (ao vivo ~1 s mais lento) |
| `DVR_IMAGE_PRESET` | `none` | `none`/`night`/`ir`/`bright`/`sharp`/`mirror`/`flip`/`rotate180` |
| `DVR_VIDEO_FILTERS` | *(vazio)* | Cadeia FFmpeg livre, ex.: `eq=saturation=1.2,unsharp=5:5:1.0` |
| `DVR_V4L2_CTRLS` | *(vazio)* | Controles da câmera, ex.: `brightness=128,contrast=32` |
| `DVR_SEGMENT_SECONDS` | `600` | Duração de cada arquivo gravado |
| `DVR_RETENTION_SECONDS` | `86400` | Janela de retenção (24 h) |
| `DVR_RECORD_SOURCE` | `hls` | `hls` (remux, recomendado) ou `device` |

## Como testar cada funcionalidade

- **Ao vivo:** abra `http://IP:2310` — o vídeo deve aparecer com o selo "AO VIVO".
- **Gravação:** aguarde ~10 min e recarregue a lista de gravações; clique para baixar.
- **Retenção:** `docker compose exec dvr ls -lh /app/recordings` — no máximo ~24 h
  de arquivos; os mais antigos são apagados automaticamente.
- **Túnel:** abra `http://IP:2310/tunnel`, ligue o toggle, aguarde a URL
  `https://…trycloudflare.com` e clique em **Copiar link**.
- **Logs:** `docker compose logs -f dvr`.

## Filtros de imagem e controles da câmera

**Filtros digitais** (aplicados no encoder do HLS; a gravação herda por ser um
remux, sem custo extra de CPU):

```dotenv
DVR_IMAGE_PRESET=ir          # none|night|ir|bright|sharp|mirror|flip|rotate180
DVR_VIDEO_FILTERS=eq=saturation=1.2,unsharp=5:5:1.0   # opcional, soma ao preset
```

- `night` — clareia e reduz ruído (cena escura).
- `ir` — alto contraste **sem cor**, simulando câmera com infravermelho ligado.
- `mirror`/`flip`/`rotate180` — corrija a orientação da imagem.

**Controles da câmera** (`v4l2-ctl`, dependem do que o driver expõe): consulte o
que a sua webcam aceita em `http://IP:2310/api/camera/controls` e então configure:

```dotenv
DVR_V4L2_CTRLS=brightness=128,contrast=32,white_balance_automatic=1
```

> Um filtro de corte de infravermelho **físico** normalmente não é controlável por
> software. Se a sua câmera expuser um controle de IR no `--list-ctrls`, ele
> aparecerá nesse endpoint e poderá ser ajustado via `DVR_V4L2_CTRLS`.

Na **página principal** há um seletor de filtro que aplica a troca na hora (sem
redeploy): ele chama `POST /api/camera/preset` e reinicia o stream por alguns
segundos. A escolha vale até o container reiniciar (aí volta ao `DVR_IMAGE_PRESET`).

### Diagnóstico rápido

- `GET /healthz` → inclui `hls_last_error`/`rec_last_error` (última mensagem de cada
  FFmpeg) e o preset/filtro ativos. É o primeiro lugar a olhar se aparecer
  "Sem sinal da câmera".
- `GET /api/camera/controls` → controles V4L2 expostos pela câmera.
- Se um filtro quebrar o stream, o backend **desativa os filtros automaticamente**
  e registra o motivo no log; ao reiniciar, volta a funcionar.

## Melhorar a qualidade da gravação

Como a gravação é um remux do HLS, **a qualidade gravada = qualidade do HLS**.
Ajuste por variáveis de ambiente:

```dotenv
DVR_VIDEO_SIZE=1280x720      # mais detalhe (use se o Celeron aguentar)
DVR_VIDEO_BITRATE=2000k      # menos artefatos de compressão
DVR_FRAMERATE=20
DVR_X264_TUNE=               # vazio = melhor compressão, +~1s de latência no ao vivo
```

As gravações usam **MP4 fragmentado**, portanto abrem normalmente **mesmo
enquanto estão sendo gravadas** e sobrevivem a reinícios. O segmento em
andamento aparece como "● gravando" na lista.

## Decisões de design

- **Container único:** backend, FFmpeg e cloudflared juntos evitam problemas de
  permissão de dispositivo entre containers e simplificam o deploy.
- **Gravação lê o HLS (`-c copy`):** uma webcam USB só aceita **um** processo
  lendo o dispositivo. O gravador consome o stream HLS local em vez de abrir
  `/dev/video0` de novo — isso também evita um segundo encode e economiza CPU.
- **Retenção no app:** uma task asyncio apaga os segmentos mais antigos com base
  no timestamp do nome do arquivo, sem depender de `cron` externo. O segmento
  em gravação nunca é apagado.
- **Túnel no backend:** o `cloudflared` roda como subprocesso; a URL é lida do
  `stderr` por regex e o processo é encerrado após 4 h.
