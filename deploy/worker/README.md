# Worker + demo de volúmenes

Este directorio despliega el **Worker Agent** junto con cuatro servicios demo.
El Control Plane se despliega por separado desde `deploy/control-plane/`.

## Qué incluye

Al levantar cualquiera de los dos Compose, el worker y los cuatro servicios demo
quedan en el mismo Compose project. El worker los descubre mediante las labels de
Docker Compose y reporta sus volúmenes al Control Plane.

| Servicio | Imagen | Volúmenes nombrados | Propósito |
| --- | --- | --- | --- |
| `demo-app` | `nginx:alpine` | `demo_nginx_html:/usr/share/nginx/html`; `demo_nginx_cache:/var/cache/nginx` | HTML y caché de nginx; se publica en `8082` solo en el Compose local |
| `demo-postgres` | `postgres:16-alpine` | `demo_postgres_data:/var/lib/postgresql/data` | Datos persistentes de una base demo |
| `demo-redis` | `redis:7-alpine` | `demo_redis_data:/var/lib/redis` | Persistencia AOF de Redis |
| `demo-files` | `alpine:3.20` | `demo_files_data:/demo-files` | Volumen sencillo con archivos deterministas para probar selección y backup |

Los nombres de servicios y volúmenes son equivalentes en `docker-compose.yml` y
`docker-compose.ghcr.yml`, por lo que el inventario es comparable entre la
variante local y la de GHCR. El volumen `worker_state` conserva las credenciales
y el estado del worker; no es parte de los datos demo.

Cada ruta de montaje aparece en el inventario como un `volume_target`
seleccionable. La UI muestra la ruta y, cuando está disponible, el nombre Docker
del volumen. En este demo, Redis aparece en `/var/lib/redis`, `demo-files` en
`/demo-files` y el estado del worker en `/data`.

Los servicios demo tienen la label
`docker-volume-backup.stop-during-backup: "true"`, para que los backups fríos
puedan detenerlos y volverlos a iniciar.

`demo-redis` usa root únicamente para que el entrypoint oficial repare los
permisos iniciales del volumen; después Redis se ejecuta como el usuario
`redis`. Es un comportamiento de bootstrap exclusivo de este demo.

## Conexión y arranque

El worker usa por defecto la red externa
`docker-volume-backup-control-plane_default` y alcanza el CP mediante
`http://control-plane:8080`. Levanta primero el Control Plane. Después configura
el token HMAC de enrolamiento en el entorno y levanta una variante:

```powershell
$env:WORKER_ENROLLMENT_TOKEN="<token-de-enrolamiento>"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Para usar las imágenes publicadas:

```powershell
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

El Compose local conserva `${DEMO_APP_PORT:-8082}:80` para nginx. No se publican
puertos de host para Postgres, Redis ni `demo-files`.

La contraseña de Postgres es explícitamente **solo para demo**. El valor por
defecto es `demo-only-password`; sobrescríbelo sin guardar secretos en el repo:

```powershell
$env:DEMO_POSTGRES_PASSWORD="otra-clave-solo-para-pruebas"
```

## Validación rápida

1. Enrola el worker y espera a que aparezca como conectado en el Control Plane.
2. Consulta `GET /api/v1/workers/{id}/inventory` para comprobar que aparecen los
   cuatro contenedores demo, el Compose project y sus `volume_targets`.

   ```powershell
   curl -b cp-cookie.txt "http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory"
   ```

3. Abre **Targets**, crea o edita un target para ese Compose project y selecciona
   solo algunos destinos: por ejemplo `/var/lib/redis` de `demo-redis`,
   `/demo-files` de `demo-files` o `/data` de `worker_state`. El selector usa
   rutas de montaje, no nombres Docker de volumen; no selecciones todos por
   defecto si estás probando la selección parcial.
4. Verifica que el target conserva únicamente los `volume_targets` elegidos.

Comprobaciones básicas del worker:

```powershell
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

El endpoint `/healthz` informa `status=ok` cuando el último contacto con el CP
fue exitoso y `status=degraded` cuando el proceso sigue vivo pero falló ese
contacto.

## Volúmenes y apagado

Los volúmenes nombrados (`demo_*`) son la forma recomendada para este ejemplo:
el worker puede montarlos en el runtime y hacer backup por archivo. Un bind mount
también puede descubrirse, pero depende de que la ruta del host sea accesible y
consistente; el demo actual usa únicamente volúmenes nombrados.

Para detener el stack sin borrar datos:

```powershell
docker compose -f deploy/worker/docker-compose.yml down
```

Mientras pruebas el inventario y la selección, **evita `docker compose down -v`**:
ese comando borra `demo_nginx_*`, `demo_postgres_data`, `demo_redis_data`,
`demo_files_data` y `worker_state`.

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL`, por
ejemplo `http://192.168.1.10:18080`. El worker requiere acceso real a
`/var/run/docker.sock` para descubrir contenedores y volúmenes.
