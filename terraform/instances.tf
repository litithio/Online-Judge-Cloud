# Feste Namen statt eines Präfixes je Person, seit #299 gibt es einen Cluster
# für die Gruppe. Das Kursprojekt teilen sich alle Gruppen des Kurses, judge
# ist dort der Name dieses Projekts. Nova lässt gleiche Instanznamen zu und
# ein Keypair gehört dem Benutzerkonto, die Namen allein verhindern also
# keinen zweiten Cluster. Dass ein Apply den bestehenden ersetzt statt einen
# zweiten daneben zu stellen, hält allein der Terraform-State der
# betreibenden Person.
#
# Der öffentliche Teil kommt aus var.ssh_public_key (terraform.tfvars), der
# private bleibt bei der betreibenden Person. Alle weiteren Schlüssel trägt
# das Play in ansible/deploy.yaml ein.
resource "openstack_compute_keypair_v2" "keypair" {
  name       = "judge-keypair"
  public_key = var.ssh_public_key
}

# Eigene Security Group statt der default-Gruppe des Kursprojekts, die alles
# durchlässt (#213). Die Gruppe selbst trägt keine Regeln, jede Regel ist eine
# eigene Ressource. Die Egress-Regeln, die Neutron jeder neuen Gruppe mitgibt,
# bleiben stehen, darüber ziehen die Nodes Pakete, Images und Charts.
resource "openstack_networking_secgroup_v2" "nodes" {
  name        = "judge-k3s-nodes"
  description = "k3s-Nodes des Online-Judge-Clusters"
}

# Zwischen den Nodes alles, sonst fällt der VXLAN-Verkehr auf UDP 8472 weg
# und Pod-Verkehr über Node-Grenzen läuft ins Leere, obwohl alle Nodes Ready
# melden (#7). Je einmal IPv4 und IPv6, weil DHBWV6 dual-stack ist und nicht
# belegt ist, über welche Familie Flannel die Tunnel aufbaut.
resource "openstack_networking_secgroup_rule_v2" "intern" {
  for_each          = toset(["IPv4", "IPv6"])
  direction         = "ingress"
  ethertype         = each.value
  remote_group_id   = openstack_networking_secgroup_v2.nodes.id
  security_group_id = openstack_networking_secgroup_v2.nodes.id
}

# Von außen nur über IPv6, die private IPv4 der Nodes liegt hinter NAT und
# bekommt von außen nichts herein. 22 für Ansible, 80 und 443 für Traefik und
# die HTTP-01-Challenge, 6443 für die API, auf die die kubeconfig zeigt. Die
# Regel gilt für alle Nodes, auf den Agents lauscht auf 6443 nichts.
resource "openstack_networking_secgroup_rule_v2" "extern" {
  for_each          = toset(["22", "80", "443", "6443"])
  direction         = "ingress"
  ethertype         = "IPv6"
  protocol          = "tcp"
  port_range_min    = tonumber(each.value)
  port_range_max    = tonumber(each.value)
  remote_ip_prefix  = "::/0"
  security_group_id = openstack_networking_secgroup_v2.nodes.id
}

# Direkt am geteilten DHBWV6-Netz wie in der GridFlex-Übung. security_groups
# erwartet Namen, keine IDs.
resource "openstack_compute_instance_v2" "server" {
  name            = "judge-k3s-server"
  image_id        = var.image_id
  flavor_name     = var.flavor_server
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = [openstack_networking_secgroup_v2.nodes.name]

  network { name = var.node_network }

  timeouts { create = "10m" }
}

# Die Dienste-Nodes tragen MongoDB, Valkey, Keycloak und die API. Der Name
# steckt in der Rolle, weil #66 die Nodes darüber kennzeichnet und die Dienste
# über einen nodeSelector daran bindet.
resource "openstack_compute_instance_v2" "dienste" {
  count           = var.dienste_count
  name            = "judge-k3s-dienste-${count.index + 1}"
  image_id        = var.image_id
  flavor_name     = var.flavor_dienste
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = [openstack_networking_secgroup_v2.nodes.name]

  network { name = var.node_network }

  timeouts { create = "10m" }
}

# Die Judge-Nodes führen eingereichten Code aus und tragen sonst nichts. Ein
# Ausbruch aus gVisor erreicht damit den Node, aber weder MongoDB noch
# Keycloak, die auf den Dienste-Nodes liegen.
resource "openstack_compute_instance_v2" "judge" {
  count           = var.judge_count
  name            = "judge-k3s-judge-${count.index + 1}"
  image_id        = var.image_id
  flavor_name     = var.flavor_judge
  key_pair        = openstack_compute_keypair_v2.keypair.name
  security_groups = [openstack_networking_secgroup_v2.nodes.name]

  network { name = var.node_network }

  timeouts { create = "10m" }
}
