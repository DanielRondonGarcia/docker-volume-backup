# Worker + Servicios a Backupear

Este directorio contiene el despliegue del **Worker Agent** junto con los
servicios que se quieren backupear.

El Control Plane se despliega por separado desde `deploy/control-plane/`.

## Filosofia

El worker se ejecuta en el mismo compose project que los servicios a los que
se les hara backup. Esto permite que:

- el worker detecte automaticamente el compose project via las labels
  `com.docker.compose.project` de los contenedores,
- el worker reporte al Control Plane el nombre del proyecto, sus volumenes
  y si tiene acceso al daemon de Docker,
- al crear un target en la UI solo necesites seleccionar el worker y el
  compose project; los `volume_targets` y `runtime_volumes` se derivan
  automaticamente del inventario.

No se requiere `network_mode` porque el backup es solo de archivos.

## Archivos

- `docker-compose.yml`: construye localmente la imagen dedicada de `worker` e incluye un servicio de ejemplo.
- `docker-compose.ghcr.yml`: consume la imagen publicada en GHCR para `worker`.

## Como anadir tus servicios

Edita `docker-compose.yml` (o `docker-compose.ghcr.yml`) y anade tus servicios
con sus volumenes. Por ejemplo:

```yaml
services:
  mi-app:
    image: mi-app:latest
    restart: unless-stopped
    volumes:
      - mi_app_data:/var/lib/app

  worker:
    # ... configuracion del worker (ya presente en el archivo)
```

El worker descubrira `mi-app` y su volumen `mi_app_data` como parte del
compose project y los reportara al Control Plane.

## Arranque

Primero levanta el Control Plane (ver `deploy/control-plane/README.md`).

Luego levanta este stack del worker apuntando al CP:

```bash
$env:CONTROL_PLANE_URL="http://control-plane:8080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Si el Control Plane esta en otro host o puerto, ajusta `CONTROL_PLANE_URL`:

```bash
$env:CONTROL_PLANE_URL="http://192.168.1.10:18080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

> Si ambos composes corren en el mismo host de Docker, el worker puede
> alcanzar al CP por el nombre del servicio `control-plane` solo si estan en
> la misma red. Por defecto cada compose crea su propia red, asi que lo
> habitual es apuntar `CONTROL_PLANE_URL` al host:puerto publicado del CP.

## Verificacion rapida

Healthcheck del worker:

```bash
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

El `healthcheck` del contenedor worker valida que el endpoint local `/healthz`
responda. Ese endpoint devuelve ademas si el ultimo contacto con el Control
Plane fue exitoso:

- `status=ok`: el worker esta vivo y alcanzo al CP en el ultimo ciclo
- `status=degraded`: el worker sigue vivo, pero no logro contactar al CP en el ultimo intento

Un estado degradado no implica que el proceso haya muerto; sirve como observabilidad adicional.

Confirmar que el worker se registro en el CP:

```bash
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

Confirmar el inventario reportado por el worker (incluye este compose project
con sus volumenes):

```bash
# $WORKER_ID se obtiene del endpoint /api/v1/workers
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory
```

## Uso con imagenes publicadas en GHCR

```bash
$env:CONTROL_PLANE_URL="http://control-plane:8080"
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

Imagen esperada:

- `ghcr.io/danielrondongarcia/docker-volume-backup-worker`

## Apagado

```bash
docker compose -f deploy/worker/docker-compose.yml down
```

Para borrar tambien los volumenes del stack (incluido el volumen de ejemplo
`demo_app_data`):

```bash
docker compose -f deploy/worker/docker-compose.yml down -v
```

## Notas

- El worker depende de acceso real a `docker.sock`; si el host no expone ese socket, el inventario y la ejecucion de runtimes no funcionaran.
- El `worker` puede reportar `control_plane_reachable=false` en `/healthz` y seguir ejecutandose; esto indica degradacion de conectividad, no necesariamente caida del proceso.
- El servicio `demo-app` es solo un ejemplo; reemplazalo o eliminalo segun tu caso.