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

# Die Dienste-Nodes tragen MongoDB, Valkey, Keycloak und die API. Der Name
# steckt in der Rolle, weil #66 die Nodes darüber kennzeichnet und die Dienste
# über einen nodeSelector daran bindet.
resource "openstack_compute_instance_v2" "dienste" {
  count           = var.dienste_count
  name            = "${var.prefix}-k3s-dienste-${count.index + 1}"
  image_id        = var.image_id
  flavor_name     = var.flavor_dienste
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = ["default"]

  network { name = var.node_network }

  timeouts { create = "10m" }
}

# Die Judge-Nodes führen eingereichten Code aus und tragen sonst nichts. Ein
# Ausbruch aus gVisor erreicht damit den Node, aber weder MongoDB noch
# Keycloak, die auf den Dienste-Nodes liegen.
resource "openstack_compute_instance_v2" "judge" {
  count           = var.judge_count
  name            = "${var.prefix}-k3s-judge-${count.index + 1}"
  image_id        = var.image_id
  flavor_name     = var.flavor_judge
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = ["default"]

  network { name = var.node_network }

  timeouts { create = "10m" }
}
