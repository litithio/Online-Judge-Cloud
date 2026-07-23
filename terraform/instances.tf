# Der öffentliche Teil kommt aus var.ssh_public_key (terraform.tfvars), der
# private bleibt auf dem eigenen Rechner.
resource "openstack_compute_keypair_v2" "keypair" {
  name       = "${var.prefix}-keypair"
  public_key = var.ssh_public_key
}

# Direkt am geteilten DHBWV6-Netz, security_groups auf "default" wie in der
# GridFlex-Übung; die default-Gruppe des Kursprojekts ist derzeit vollständig
# offen.
resource "openstack_compute_instance_v2" "server" {
  name            = "${var.prefix}-k3s-server"
  image_id        = var.image_id
  flavor_name     = var.flavor_server
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = ["default"]

  network { name = var.node_network }

  timeouts { create = "10m" }
}

# var.worker_count ist die einzige Stelle, an der sich die Anzahl ändert; drei
# ist das Minimum, bei dem Verteilung (P4) und Skalierung (P6) sichtbar werden.
resource "openstack_compute_instance_v2" "worker" {
  count           = var.worker_count
  name            = "${var.prefix}-k3s-worker-${count.index + 1}"
  image_id        = var.image_id
  flavor_name     = var.flavor_worker
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = ["default"]

  network { name = var.node_network }

  timeouts { create = "10m" }
}
