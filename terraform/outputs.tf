locals {
  agents    = concat(openstack_compute_instance_v2.dienste, openstack_compute_instance_v2.judge)
  all_nodes = concat([openstack_compute_instance_v2.server], local.agents)

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

output "dienste_ips" {
  value = [for n in openstack_compute_instance_v2.dienste : local.conn_addr[n.name]]
}

output "judge_ips" {
  value = [for n in openstack_compute_instance_v2.judge : local.conn_addr[n.name]]
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
          # ansible_python_interpreter, nicht interpreter_python. Der zweite
          # Name ist der ini-Schlüssel für ansible.cfg, als Inventory-Variable
          # wirkt er nicht und Ansible fiele auf die eigene Suche zurück.
          vars = { ansible_python_interpreter = "/usr/bin/python3" }
          children = {
            judge_k3s_server = {
              vars  = { k3s_role = "server" }
              hosts = { (local.master_addr) = { ansible_user = "ubuntu" } }
            }
            # Die beiden Untergruppen tragen die Rolle aus #88. Die k3s-Rolle
            # sieht weiterhin nur judge_k3s_agent, die Untergruppen erben ihre
            # Variablen. Sie sind die Handhabe für #66, das gVisor und die
            # Labels auf die Judge-Nodes eingrenzt.
            judge_k3s_agent = {
              vars = {
                k3s_role        = "agent"
                k3s_server_host = local.master_addr
              }
              children = {
                judge_k3s_agent_dienste = {
                  hosts = { for n in openstack_compute_instance_v2.dienste :
                    local.conn_addr[n.name] => { ansible_user = "ubuntu" }
                  }
                }
                judge_k3s_agent_judge = {
                  hosts = { for n in openstack_compute_instance_v2.judge :
                    local.conn_addr[n.name] => { ansible_user = "ubuntu" }
                  }
                }
              }
            }
          }
        }
      }
    }
  })
}
