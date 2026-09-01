{{/*
Gemeinsame Bausteine des Charts. Ausgelagert, damit die Labels an jedem Objekt
gleich lauten und die Verbindungs-URLs nur an einer Stelle stehen.
*/}}

{{/*
Name des Release, gekürzt auf die 63 Zeichen, die Kubernetes für Namen zulässt.
*/}}
{{- define "online-judge.name" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels an jedem Objekt. app.kubernetes.io/* sind die empfohlenen Standardlabels;
managed-by und instance machen sichtbar, dass Helm das Objekt hält und zu
welchem Release es gehört.
*/}}
{{- define "online-judge.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: online-judge
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Adresse der Queue als host:port mit vollem DNS-Namen, für den KEDA-Trigger,
der aus dem Namespace keda heraus verbindet und deshalb nicht mit dem bloßen
Servicenamen auskommt. Valkey muss dafür im Namespace des Release liegen.
Baut aus redisService und redisPort in den values (externe).
Die REDIS_URI für Backend, Worker und durchlauf kommt nicht von hier, sie
liegt samt Passwort im Secret aus redisSecret und wird per secretKeyRef
gelesen, genau wie die MongoDB-URI aus dem Operator-Secret.
*/}}
{{- define "online-judge.redisKedaAdresse" -}}
{{- printf "%s.%s.svc.cluster.local:%v" .Values.externe.redisService .Release.Namespace .Values.externe.redisPort -}}
{{- end -}}

{{/*
Voller Image-Verweis eines eigenen Dienstes: registry/repository:tag. Aufruf mit
einem dict aus dem Wurzelkontext und dem repository, z. B.
{{ include "online-judge.image" (dict "root" . "repo" .Values.backend.repository) }}
*/}}
{{- define "online-judge.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .repo .root.Values.image.tag -}}
{{- end -}}
