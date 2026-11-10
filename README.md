# DVR — Câmera USB ou IP (icsee/yoosee) com HLS, gravação 24 h e Quick Tunnel

Sistema de DVR caseiro para **uma webcam USB** (`/dev/video0`) **ou uma câmera IP
icsee/Yoosee via RTSP** (escolha na variável `DVR_MODO_OP`), em **um único
container** Docker:

- **Stream ao vivo** via HLS (funciona em qualquer navegador; player `hls.js`).
- **Gravação cíclica** em segmentos MP4 de 10 minutos com **retenção de 24 h**,
  com **botão de download** para cada gravação.
- **Cloudflare Quick Tunnel** sob demanda, com expiração automática em 4 h.
- **Autenticação HTTP Basic** em tudo, exceto o próprio stream HLS.

Feito para hardware modesto (Celeron N3350 / 4 GB RAM) e Ubuntu Server 24.04.
Porta principal: **2310**.

## Estrutura

```
.
├── docker-compose.yml         # orquestração única (EasyPanel ou `docker compose up`)
├── Dockerfile                 # Python 3.12 + FFmpeg + cloudflared
├── requirements.txt
├── app/
│   ├── main.py                # FastAPI: rotas, auth, ciclo de vida
│   ├── config.py              # TODAS as variáveis de ambiente
│   ├── streamer.py            # FFmpeg (USB ou RTSP) -> HLS ao vivo (+ watchdog)
│   ├── recorder.py            # FFmpeg -> segmentos MP4 + retenção 24 h
│   ├── tunnel.py              # subprocesso cloudflared (+ TTL de 4 h)
│   └── procutils.py           # encerramento de subprocessos
├── static/                    # index.html, tunnel.html, style.css, app.js
├── recordings/                # (volume) gravacoes .mp4
└── hls/                       # (tmpfs) segmentos HLS ao vivo
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

> **Câmera IP (icsee/yoosee):** nenhum device é necessário — o compose principal
> já sobe em qualquer host. Configure as variáveis `DVR_MODO_OP`/`DVR_IPCAM_*`
> no `.env` (ou no Environment do EasyPanel).
>
> **Webcam USB (`DVR_MODO_OP=usb`):** é preciso existir `/dev/video0` no host e
> **descomentar** o bloco `devices`/`group_add` no fim do `docker-compose.yml` —
> o Docker falha ao criar o container se o device for mapeado sem existir
> (confirme no host com `ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 teste.jpg`).

## Modos de operação (`DVR_MODO_OP`)

| Modo | Fonte | Como funciona |
|---|---|---|
| `usb` | Webcam USB `/dev/video0` | Encoda com x264 e aplica filtros (comportamento original) |
| `icsee` | Câmera IP iCSee/Xmeye via RTSP | **Copia** o H.264 da câmera (`-c copy`, custo de CPU ~zero); re-encode só com filtro configurado |
| `yoosee` | Câmera IP Yoosee via RTSP | Igual ao `icsee`, com URL/fluxos do Yoosee |

Nos três modos a **gravação é idêntica na prática**: o gravador sempre remuxa o
HLS local em MP4s de 10 min com a mesma retenção — só a *entrada* do streamer
muda.

### Configurando a câmera IP (icsee/yoosee)

```dotenv
DVR_MODO_OP=icsee            # icsee | yoosee
DVR_IPCAM_HOST=192.168.1.15  # IP da câmera
DVR_IPCAM_PORT=              # vazio = padrão do modo: icsee 554 | yoosee 554
DVR_IPCAM_USER=admin
DVR_IPCAM_PASS=sua_senha
DVR_IPCAM_CHANNEL=1          # canal do RTSP (1 = 1ª câmera; no app iCSee ela é "0")
DVR_IPCAM_STREAM=0           # 0 = principal | 1 = secundário (menor resolução)
```

URLs geradas automaticamente (montadas em `app/config.py`):

- **icsee/Xmeye** → `rtsp://user:senha@IP:554/user=admin&password=senha&channel=1&stream=0.sdp?real_stream`
  (formato oficial do Xiongmai; cuidado com a porta **34567 — é o port "DVRIP/media"
  do app iCSee, não serve RTSP**; e o canal do RTSP é 1-based).
- **yoosee** → `rtsp://user:senha@IP:554/onvif1` (`onvif2` quando `DVR_IPCAM_STREAM=1`).

Caracteres especiais na senha são escapados automaticamente. Para firmwares
exóticos, monte a URL na mão com `DVR_IPCAM_URL` (ela sobrescreve todas as
outras; ex.: `rtsp://admin:senha@IP:10554/tcp/av0_0`). Senhas NUNCA aparecem em
logs/APIs — sempre mascaradas como `***`. Definir `DVR_IPCAM_URL` desliga o
auto-probe (abaixo).

### Auto-probe da câmera IP (DVR_AUTO_PROBE, padrão ligado nos modos IP)

Se o FFmpeg falhar 3× seguidas ao abrir a câmera, o DVR testa com `ffprobe`
uma lista curta de combinações porta/canal — entre elas `554/1`, `554/0` e
`34567/1` — e usa a primeira que responder, registrando em memória (um redeploy
volta a respeitar o env). O resultado aparece no log como `auto-probe: RTSP
ABRIU em porta 554 canal 1` e o `/healthz` marca o rótulo `(auto-probe)`.
Câmera cloud-only (sem RTSP válido de jeito nenhum)? O log indica usar uma
ponte **dvrip:// → RTSP** (ex.: [go2rtc](https://github.com/AlexxIT/go2rtc))
e apontar `DVR_IPCAM_URL` para ela. `DVR_AUTO_PROBE=0` desliga.

Detalhes úteis:

- `DVR_IPCAM_TRANSPORT=tcp` (padrão) evita artefatos em Wi-Fi. `udp` pode dar
  menos latência, mas perde pacotes.
- `DVR_IPCAM_TRANSCODE=auto` (padrão): **copia** o H.264 da câmera; se você
  configurar um preset de filtro (`DVR_IMAGE_PRESET`/`DVR_VIDEO_FILTERS`), o
  streamer re-encoda com os filtros (e a gravação herda). `=1` força re-encode
  sempre (pesado no Celeron N3350); `=0` nunca re-encode (filtros sem efeito).
- Falha de RTSP aparece como `Connection refused`/`timed out` em
  `docker compose logs -f dvr` e em `GET /healthz` (`hls_last_error`).

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `DVR_MODO_OP` | `usb` | `usb` (webcam) \| `icsee` \| `yoosee` (câmera IP via RTSP) |
| `DVR_IPCAM_HOST` | *(vazio)* | IP da câmera IP (aceita `ip` ou `ip:porta`) |
| `DVR_IPCAM_PORT` | por modo | Porta RTSP: `icsee`→`554`, `yoosee`→`554` (a 34567 é o port do app iCSee, não RTSP) |
| `DVR_IPCAM_USER` | `admin` | Usuário da câmera IP |
| `DVR_IPCAM_PASS` | *(vazio)* | Senha da câmera IP (mesma do app iCSee/Yoosee) |
| `DVR_IPCAM_CHANNEL` | `1` | Canal (NVR/1ª câmera) |
| `DVR_IPCAM_STREAM` | `0` | `0` = fluxo principal, `1` = secundário |
| `DVR_IPCAM_TRANSPORT` | `tcp` | Transporte RTSP (`tcp` é estável em Wi-Fi) |
| `DVR_IPCAM_TRANSCODE` | `auto` | `auto`=copiar H.264 (re-encode só com filtro) \| `1` \| `0` |
| `DVR_AUTO_PROBE` | `1` | Modos IP: após 3 falhas do FFmpeg, testa porta/canal com `ffprobe` e usa a 1ª que abrir |
| `DVR_IPCAM_URL` | *(vazio)* | URL RTSP completa que sobrescreve tudo (firmwares exóticos) — desliga o auto-probe |
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
- **Gravação:** aguarde ~10 min e recarregue a lista de gravações; clique em
  **⬇ Baixar** (ou no nome do arquivo) para salvar o MP4 — inclusive o segmento
  em gravação, que já abre por ser MP4 fragmentado.
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

- `GET /healthz` → inclui `mode`/`camera` (modo ativo e fonte de vídeo),
  `hls_last_error`/`rec_last_error` (última mensagem de cada FFmpeg) e o
  preset/filtro ativos. É o primeiro lugar a olhar se aparecer "Sem sinal da
  câmera".
- `GET /api/camera/info` → modo, rótulo da câmera e URL RTSP ativa (senha
  mascarada).
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

> **No modo IP** a fonte é o H.264 da câmera: escolha o fluxo com
> `DVR_IPCAM_STREAM` (`0` principal / `1` secundário) e, se o streamer estiver
> re-encodando (com filtro), o bitrate do re-encode é `DVR_VIDEO_BITRATE`.

As gravações usam **MP4 fragmentado**, portanto abrem normalmente **mesmo
enquanto estão sendo gravadas** e sobrevivem a reinícios. O segmento em
andamento aparece como "● gravando" na lista.

## Decisões de design

- **Container único:** backend, FFmpeg e cloudflared juntos evitam problemas de
  permissão de dispositivo entre containers e simplificam o deploy.
- **Modo de operação atrás de uma env** (`DVR_MODO_OP`): o pipeline único
  (HLS -> remux MP4) não muda com a fonte; apenas a entrada do streamer troca
  (v4l2 vs RTSP). Assim o modo USB original continua disponível sem regressão.
- **No modo IP o H.264 da câmera é copiado (`-c copy`):** o Celeron N3350 não
  re-encoda 720p/1080p em tempo real. Re-encode só ocorre sob demanda (filtros
  de imagem), herdeiro do mesmo mecanismo do modo USB.
- **Gravação lê o HLS (`-c copy`):** uma webcam USB só aceita **um** processo
  lendo o dispositivo. O gravador consome o stream HLS local em vez de abrir
  `/dev/video0` de novo — isso também evita um segundo encode e economiza CPU.
- **Retenção no app:** uma task asyncio apaga os segmentos mais antigos com base
  no timestamp do nome do arquivo, sem depender de `cron` externo. O segmento
  em gravação nunca é apagado.
- **Túnel no backend:** o `cloudflared` roda como subprocesso; a URL é lida do
  `stderr` por regex e o processo é encerrado após 4 h.
