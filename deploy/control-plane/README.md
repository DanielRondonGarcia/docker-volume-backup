# Control Plane — Despliegue Docker

Este directorio contiene el despliegue **solo del Control Plane**.

El Worker Agent se despliega por separado, junto con los servicios que se
quieren backupear, en `deploy/worker/`.

## Archivos

- `docker-compose.yml`: construye localmente la imagen dedicada de
  `control-plane`.
- `docker-compose.ghcr.yml`: consume la imagen publicada en GHCR para
  `control-plane`.

## Comportamiento

- `control-plane`
  - ejecuta `python -m src.control_plane.main`
  - evalua las expresiones cron en `CONTROL_PLANE_TIMEZONE` (por defecto
    `America/Bogota`); usa una zona IANA valida y expone esa zona a la UI/API
  - persiste `control_plane.db`, `.control_plane.key`,
    `.control_plane.session.key` y `.control_plane.users.json` en un volumen
    Docker nombrado `control_plane_state`
  - publica la UI/API en el puerto definido por `CONTROL_PLANE_PUBLISHED_PORT`
    (por defecto `8080`) y mantiene `8080` interno dentro de la red del Compose
- `redis`
  - ejecuta Redis 7 para la cache acotada de metadatos de Snapshot Explorer v2,
    con AOF en el volumen nombrado `snapshot_explorer_redis`
  - conserva las entradas hasta 86400 segundos (24 horas) por defecto; el Worker
    aplica ese TTL con un maximo de 86400 segundos y una cardinalidad maxima de
    1000 entradas por target y repositorio
  - mantiene `maxmemory 128mb` con politica `allkeys-lru`: la persistencia AOF y
    el volumen no evitan la expiracion, la eviccion por memoria ni los limites de
    cardinalidad
  - solo es accesible dentro de la red del Compose; el Worker lo alcanza como
    `redis:6379` a través de la red compartida

## Red compartida con el Worker

El Compose del Control Plane crea una red llamada
`docker-volume-backup-control-plane_default`. El Compose del Worker se conecta
a esa red como red externa, por lo que el Worker alcanza al CP por el nombre del
servicio `control-plane` sin necesidad de exponer el puerto del CP al host.

Por eso el valor por defecto de `CONTROL_PLANE_URL` en el Worker es
`http://control-plane:8080`.

El Worker usa por defecto `redis://redis:6379/0` para las lecturas de metadatos
del explorador. Para desactivar Redis explícitamente, conserva la variable vacía
al levantar el stack del Worker:

```powershell
$env:SNAPSHOT_EXPLORER_REDIS_URL=""
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Si Redis no está disponible, está mal configurado o se desactiva, el Worker
continúa leyendo directamente de Restic y el Control Plane mantiene su catálogo
SQLite; una lectura ordinaria no depende de Redis.

> Nota: el servicio `demo-app` del Compose del Worker usa un **bind mount**
> (`./demo-app-data`), no un volumen nombrado. El Worker detecta igualmente el
> compose project y sus rutas, pero tenlo en cuenta al añadir tus propios
> servicios: los bind mounts y los volúmenes nombrados se reportan de forma
> distinta en el inventario.

## Arranque

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

Para cambiar la zona del scheduler, define una zona IANA antes de arrancar el
stack, por ejemplo `America/Bogota`, `America/New_York` o `UTC`:

```powershell
$env:CONTROL_PLANE_TIMEZONE="America/Bogota"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

Una zona invalida impide que el Control Plane arranque para evitar ejecutar
backups en una hora distinta de la configurada.

Si el Control Plane va a estar detrás de un proxy o con un dominio, define
también la URL pública para que los comandos del worker salgan correctos:

```powershell
$env:CONTROL_PLANE_PUBLIC_URL="https://backups.miempresa.com"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

## Verificación rápida

Salud del Control Plane:

```powershell
curl http://127.0.0.1:18080/healthz
```

Primer login:

- usuario: `admin`
- contraseña inicial: `changeme`
- el sistema obliga a cambiarla en el primer acceso

```powershell
curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

Listar workers una vez cambiada la contraseña:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

## Primeros pasos después del arranque

Tras levantar el Control Plane y el Worker y hacer el primer login:

1. **Cambiar la contraseña** del usuario `admin` (obligatorio en el primer
   acceso).
2. **Crear un secreto** `rclone.conf` (tipo `file`) con la configuración del
   remoto donde se guardarán los backups. Opcionalmente un secreto de tipo
   `env` con la contraseña de Restic.
3. **Crear un storage profile** que referencie el secreto anterior y defina el
   `RESTIC_REPOSITORY` y la estrategia de backend (`restic-rclone`).
4. **Crear un target** seleccionando el worker y el compose project. Los
   `volume_targets` y `runtime_volumes` se derivan automáticamente del
   inventario del Worker; asocia el storage profile y, si quieres, una política
   de retención.
5. **Disparar un backup** desde el botón de la UI del target y revisar el job
   resultante en la vista de jobs.

## Uso con imágenes publicadas en GHCR

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Imagen esperada:

- `ghcr.io/danielrondongarcia/docker-volume-backup-control-plane`

## Enrolamiento HMAC y transporte

Release mayor sin migración: configura desde cero y conserva el `WORKER_ID` explícito.
La UI genera el secreto HMAC; el CP guarda el digest y el worker persiste el
archivo `/data/worker_credentials.json` (`0600`). No hay registro abierto ni
certificados de cliente, CSR o fallback. HTTP local no es confidencial; para
remoto usa HTTPS server-only con `CONTROL_PLANE_CA_FILE`. Tras cinco fallos termina.

## Apagado

```powershell
docker compose -f deploy/control-plane/docker-compose.yml down
```

## Notas

- Si `8080` ya está ocupado en el host, publica el stack en otro puerto usando
  `CONTROL_PLANE_PUBLISHED_PORT`.
- Despliega el worker desde `deploy/worker/`; por defecto usa la red externa y
  `CONTROL_PLANE_URL=http://control-plane:8080`.
