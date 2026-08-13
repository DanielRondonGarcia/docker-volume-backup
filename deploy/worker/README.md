# Worker + Servicios a Backupear

Este directorio contiene el despliegue del **Worker Agent** junto con los
servicios que se quieren backupear.

El Control Plane se despliega por separado desde `deploy/control-plane/`.

## Filosofía

El worker se ejecuta en el mismo Compose project que los servicios a los que
se les hará backup. Esto permite que:

- el worker detecte automáticamente el compose project vía las labels
  `com.docker.compose.project` de los contenedores,
- el worker reporte al Control Plane el nombre del proyecto, sus volúmenes
  y si tiene acceso al daemon de Docker,
- al crear un target en la UI solo necesites seleccionar el worker y el
  compose project; los `volume_targets` y `runtime_volumes` se derivan
  automáticamente del inventario.

No se requiere `network_mode` porque el backup es solo de archivos.

## Conexión con el Control Plane

El Compose del Worker se conecta por defecto a la red externa
`docker-volume-backup-control-plane_default`, que es la red que crea el
Compose del Control Plane. Gracias a esto, el Worker puede alcanzar al CP por
el nombre del servicio `control-plane` sin necesidad de exponer el puerto del
CP al host.

Por eso el valor por defecto de `CONTROL_PLANE_URL` es:

```
http://control-plane:8080
```

Este es el valor recomendado cuando ambos stacks corren en el mismo host de
Docker. **No** uses `http://host.docker.internal:18080` salvo que el CP esté en
otro host o no puedas compartir la red entre Composes.

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL`:

```powershell
$env:CONTROL_PLANE_URL="http://192.168.1.10:18080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

## Archivos

- `docker-compose.yml`: construye localmente la imagen dedicada de `worker` e
  incluye un servicio de ejemplo.
- `docker-compose.ghcr.yml`: consume la imagen publicada en GHCR para `worker`.

## Servicio de ejemplo: bind mount, no volumen nombrado

El servicio `demo-app` (nginx) incluido en el Compose usa un **bind mount**:

```yaml
volumes:
  - ./demo-app-data:/usr/share/nginx/html
```

Esto significa que los datos se guardan en el directorio `./demo-app-data` del
host, no en un volumen Docker nombrado. El Worker detecta igualmente el
compose project, pero ten en cuenta que los bind mounts y los volúmenes
nombrados se reportan de forma distinta en el inventario.

## Cómo añadir tus servicios

Edita `docker-compose.yml` (o `docker-compose.ghcr.yml`) y añade tus servicios
con sus volúmenes. Ejemplo con un volumen nombrado:

```yaml
services:
  mi-app:
    image: mi-app:latest
    restart: unless-stopped
    volumes:
      - mi_app_data:/var/lib/app

  worker:
    # ... configuración del worker (ya presente en el archivo)
```

Y declara el volumen al final del archivo:

```yaml
volumes:
  mi_app_data:
```

El worker descubrirá `mi-app` y su volumen `mi_app_data` como parte del
compose project y los reportará al Control Plane.

### Bind mount vs volumen nombrado

- **Volumen nombrado** (ej. `mi_app_data:/var/lib/app`): es la forma
  recomendada. El Worker lo gestiona como un volumen Docker real, lo monta en
  el contenedor de runtime y hace el backup por archivo.
- **Bind mount** (ej. `./mis-datos:/var/lib/app`): los datos viven en una ruta
  del host. El Worker lo reporta en el inventario, pero el montaje en el
  contenedor de runtime depende de que la ruta del host sea accesible y
  consistente. Funciona, pero requiere más cuidado en la definición del
  target.

## Arranque

Primero levanta el Control Plane (ver `deploy/control-plane/README.md`).
Luego levanta este stack del worker:

```powershell
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

No hace falta definir `CONTROL_PLANE_URL` si el CP corre en el mismo host y su
Compose ya creó la red `docker-volume-backup-control-plane_default`: el valor
por defecto `http://control-plane:8080` ya funciona.

## Verificación rápida

Healthcheck del worker:

```powershell
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

El `healthcheck` del contenedor worker valida que el endpoint local `/healthz`
responda. Ese endpoint devuelve además si el último contacto con el Control
Plane fue exitoso:

- `status=ok`: el worker está vivo y alcanzó al CP en el último ciclo.
- `status=degraded`: el worker sigue vivo, pero no logró contactar al CP en el
  último intento.

Un estado degradado no implica que el proceso haya muerto; sirve como
observabilidad adicional.

Confirmar que el worker se registró en el CP:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

Confirmar el inventario reportado por el worker (incluye este compose project
con sus volúmenes):

```powershell
# $WORKER_ID se obtiene del endpoint /api/v1/workers
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory
```

## Uso con imágenes publicadas en GHCR

```powershell
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

Imagen esperada:

- `ghcr.io/danielrondongarcia/docker-volume-backup-worker`

## Apagado

```powershell
docker compose -f deploy/worker/docker-compose.yml down
```

Para borrar también los volúmenes del stack (incluido el volumen de ejemplo
`demo_app_data`):

```powershell
docker compose -f deploy/worker/docker-compose.yml down -v
```

## Notas

- El worker depende de acceso real a `docker.sock`; si el host no expone ese
  socket, el inventario y la ejecución de runtimes no funcionarán.
- El `worker` puede reportar `control_plane_reachable=false` en `/healthz` y
  seguir ejecutándose; esto indica degradación de conectividad, no
  necesariamente caída del proceso.
- El servicio `demo-app` es solo un ejemplo; reemplázalo o elimínalo según tu
  caso.