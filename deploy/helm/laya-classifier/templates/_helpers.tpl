{{- define "laya-classifier.fullname" -}}
{{- printf "%s-laya-classifier" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "laya-classifier.labels" -}}
app.kubernetes.io/name: laya-classifier
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "laya-classifier.selectorLabels" -}}
app.kubernetes.io/name: laya-classifier
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
