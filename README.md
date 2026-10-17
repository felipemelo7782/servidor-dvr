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
| `DVR_VIDEO_BITRATE` | `800k` | Bitrate do H.264 |
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
