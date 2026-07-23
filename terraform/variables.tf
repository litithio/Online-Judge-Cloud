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

# ID statt Name wie in der GridFlex-Übung. Default ist Ubuntu Server 24.04;
# aus openstack image list, wenn das Image neu hochgeladen wird.
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

variable "flavor_worker" {
  description = "Flavor der Worker-Nodes"
  type        = string
  default     = "k8s.node"
}

variable "worker_count" {
  description = "Anzahl der Worker; P4 und P6 verlangen mehrere"
  type        = number
  default     = 3
}

# DHBWV6 ist dual-stack und shared: private IPv4 mit NAT nach außen, dazu eine
# öffentliche IPv6. Kein eigenes Netz, kein Router, keine Floating IPs, wie in
# der GridFlex-Übung.
variable "node_network" {
  description = "Geteiltes Netz, an dem die Nodes direkt hängen"
  type        = string
  default     = "DHBWV6"
}
