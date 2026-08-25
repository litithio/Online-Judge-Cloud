variable "application_credential_id" {
  description = "ID des Application Credential aus dem Web-Portal (Identity, Application Credentials)"
  type        = string
  sensitive   = true
}

variable "application_credential_secret" {
  description = "Secret des Application Credential"
  type        = string
  sensitive   = true
}

variable "prefix" {
  description = "Namenspräfix je Person, weil alle im selben Kursprojekt arbeiten"
  type        = string
}

variable "ssh_public_key" {
  description = "Öffentlicher SSH-Schlüssel für den Zugang zu allen Nodes"
  type        = string
}

variable "image_id" {
  description = "ID des Boot-Images der Nodes (Ubuntu Server 24.04)"
  type        = string
  default     = "7842eb53-0ac7-4677-9160-2466371b4302"
}

variable "flavor_server" {
  description = "Flavor des k3s-Servers"
  type        = string
  default     = "k8s.master"
}

# Zwei Rollen mit unterschiedlichem Bedarf, beschlossen in #88. Einen Judge-Node
# begrenzen die Kerne, weil jeder Judge-Worker einen ganzen Kern anfordert.
# Einen Dienste-Node begrenzt der Speicher, den MongoDB und Keycloak halten.
# Beide fahren heute denselben Flavor. Getrennte Variablen, damit eine Rolle
# ohne die andere wechseln kann.
variable "flavor_dienste" {
  description = "Flavor der Dienste-Nodes"
  type        = string
  default     = "k8s.node"
}

variable "flavor_judge" {
  description = "Flavor der Judge-Nodes"
  type        = string
  default     = "k8s.node"
}

# Drei Dienste-Nodes, weil ein MongoDB-Replica-Set den Verlust eines Nodes nur
# übersteht, wenn seine drei Members auf drei Nodes liegen. Zwei Judge-Nodes,
# damit die Platzierung und die Skalierung über Nodes hinweg wirken und nicht
# nur über Pods auf einem Node.
variable "dienste_count" {
  description = "Anzahl der Dienste-Nodes"
  type        = number
  default     = 3
}

variable "judge_count" {
  description = "Anzahl der Judge-Nodes"
  type        = number
  default     = 2
}

# DHBWV6 ist dual-stack und shared: private IPv4 mit NAT nach außen, dazu eine
# öffentliche IPv6. Kein eigenes Netz, kein Router, keine Floating IPs, wie in
# der GridFlex-Übung.
variable "node_network" {
  description = "Geteiltes Netz, an dem die Nodes direkt hängen"
  type        = string
  default     = "DHBWV6"
}
