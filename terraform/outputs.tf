locals {
  all_nodes = concat([openstack_compute_instance_v2.server], openstack_compute_instance_v2.worker)

  # Verbindungsadresse je Node: IPv6, wenn jede Node eine hat, sonst die
  # NAT-IPv4. ip_family überlässt die k3s-Rolle sich selbst (Default auto).
  cluster_has_v6 = alltrue([for n in local.all_nodes : length(n.network[0].fixed_ip_v6) > 0])
  conn_addr = { for n in local.all_nodes :
    n.name => local.cluster_has_v6 ? n.network[0].fixed_ip_v6 : n.network[0].fixed_ip_v4
  }
  master_addr = local.conn_addr[openstack_compute_instance_v2.server.name]
}

output "master_ip" {
  value = local.master_addr
}

output "worker_ips" {
  value = [for w in openstack_compute_instance_v2.worker : local.conn_addr[w.name]]
}

# Inventory im Gruppenformat der k3s-Rolle. Persönliche Werte (E-Mail, Zone,
# TSIG) gehören nicht hierher, sondern in eine group_vars-Datei bei #8. Der
# Pfad steht im .gitignore, weil er die IPs eines konkreten Deployments enthält.
resource "local_file" "ansible_inventory" {
  filename = "${path.module}/../ansible/inventory/generated-inventory.yml"
  content = yamlencode({
    all = {
      children = {
        judge = {
          vars = { interpreter_python = "/usr/bin/python3" }
          children = {
            judge_k3s_server = {
              vars  = { k3s_role = "server" }
              hosts = { (local.master_addr) = { ansible_user = "ubuntu" } }
            }
            judge_k3s_agent = {
              vars = {
                k3s_role        = "agent"
                k3s_server_host = local.master_addr
              }
              hosts = { for w in openstack_compute_instance_v2.worker :
                local.conn_addr[w.name] => { ansible_user = "ubuntu" }
              }
            }
          }
        }
      }
    }
  })
}
