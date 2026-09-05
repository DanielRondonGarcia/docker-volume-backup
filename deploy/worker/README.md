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

El cache de metadatos de Snapshot Explorer usa Redis cuando esta disponible. Su
TTL por defecto es de 86400 segundos (24 horas), con un maximo de 86400 y hasta
1000 entradas por target y repositorio. Redis conserva AOF en un volumen
nombrado, pero la expiracion, la eviccion por memoria y esos limites siguen
aplicando; el Worker vuelve a Restic si Redis no puede atender una lectura.

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

El Compose local usa por defecto la red externa
`docker-volume-backup-control-plane_default`. El Compose GHCR usa por defecto
`docker-volume-backup-control-plane-ghcr_default`. Ambas variantes alcanzan el
CP mediante `http://control-plane:8080`; levanta primero el Control Plane.
Después configura el token HMAC de enrolamiento en el entorno y levanta una
variante:

```powershell
$env:WORKER_ENROLLMENT_TOKEN="<token-de-enrolamiento>"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Para usar las imágenes publicadas:

```powershell
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

Si el worker GHCR debe emparejarse intencionalmente con otra red del CP, puedes
sobrescribir el valor por defecto antes de levantarlo:

```powershell
$env:CONTROL_PLANE_NETWORK="nombre-de-la-red-del-cp"
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

## Kubernetes worker deployment

The Kubernetes worker uses the same published worker image as the Docker
deployment, but selects the Kubernetes runtime explicitly. The first slice is
namespace-scoped: one worker installation must operate in one target namespace,
and the target must name one or more PVCs explicitly.

### Requirements and architecture

- Kubernetes 1.25 or newer is recommended, with permission to create Jobs and
  to list namespaces, PVCs, Pods, Deployments, and StatefulSets as described by
  `deploy/worker/k8s/worker.yaml`.
- Published runtime and worker images are multi-architecture manifests for
  `linux/amd64` and `linux/arm64`. The node architecture must match one of
  those platforms; no privileged Docker-in-Docker setup is required.
- The worker image includes the Kubernetes Python client. Docker remains the
  default runtime for Compose, while Kubernetes installations set
  `WORKER_RUNTIME=kubernetes`.
- Replace the example image tags with the release version when pinning a
  deployment. Keep `BACKUP_RUNTIME_IMAGE` pointed at the matching backup
  runtime image, not the worker image.

### ServiceAccount and least-privilege RBAC

The manifest creates the `backup-worker` namespace, a dedicated
`docker-volume-backup-worker` ServiceAccount, a namespace-limited Role and
RoleBinding, and a read-only ClusterRole/ClusterRoleBinding for namespace
listing. It does not grant cluster-admin access and it does not embed a bearer
token or kubeconfig. Apply it with:

```sh
kubectl apply -f deploy/worker/k8s/worker.yaml
```

The Role is intentionally bound to the namespace where the worker runs. For a
different application namespace, copy the manifest and change the namespace
consistently on the Namespace, ServiceAccount, Role, RoleBinding, Deployment,
and `WORKER_KUBERNETES_NAMESPACE` value; do not broaden the Role to the whole
cluster. The selected PVCs and their Deployment/StatefulSet owners must be in
that same namespace.

### Enrollment and Secret mapping

Create an enrollment token through the Control Plane's worker enrollment flow.
Do not commit it or place it in a manifest. After applying the non-secret
resources, create the namespace-local Secret from an environment variable:

```sh
export WORKER_ENROLLMENT_TOKEN='<token-from-control-plane>'
kubectl -n backup-worker create secret generic docker-volume-backup-worker-enrollment \
  --from-literal=token="$WORKER_ENROLLMENT_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n backup-worker rollout restart deployment/docker-volume-backup-worker
```

The Deployment maps only the Secret key into `WORKER_ENROLLMENT_TOKEN`.
Backup and restore credentials are mapped separately by operation payloads to
Kubernetes Secret references or read-only Secret files; literal values must
not be sent to the Control Plane or written into Job manifests.

### Runtime configuration and verification

The ready-to-apply Deployment sets the Kubernetes runtime, the worker image,
the backup runtime image, the in-cluster namespace, and the health probes. Set
`CONTROL_PLANE_URL` to a URL reachable from the Pod and use the published
version-matched images for production:

```sh
kubectl -n backup-worker set env deployment/docker-volume-backup-worker \
  CONTROL_PLANE_URL='http://control-plane:8080' \
  WORKER_RUNTIME=kubernetes \
  BACKUP_RUNTIME_IMAGE='ghcr.io/danielrondongarcia/docker-volume-backup:1.2.3'
kubectl -n backup-worker rollout status deployment/docker-volume-backup-worker
kubectl -n backup-worker logs deployment/docker-volume-backup-worker --tail=50
```

After enrollment, verify the worker advertises `runtime_kind=kubernetes` and
`capabilities=["kubernetes"]`, then use the Control Plane inventory to select
one namespace and explicit PVC names. The adapter fails closed when RBAC
denies inventory or a PVC is missing.

### Explicit first-slice limitations

This deployment does **not** provide live file browsing, CSI VolumeSnapshots,
multi-namespace targets, Helm charts, Operators, or CRDs. Backup and restore
use labelled Kubernetes Jobs, quiesce matching Deployments/StatefulSets, and
restore their replica counts in a bounded `finally` path. A real-cluster smoke
test is required for each target cluster; local manifest dry-runs and fake
client tests do not prove cluster RBAC or storage behavior.
