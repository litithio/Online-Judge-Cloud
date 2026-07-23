terraform {
  # 1.15.8 ist die Version, die auch die CI benutzt (siehe infra.yml); die
  # fmt-Regeln ändern sich zwischen Minor-Versionen, darum die Untergrenze.
  required_version = ">= 1.15.0"

  required_providers {
    # Beide Provider auf eine exakte Version gepinnt, weil das lock-File im
    # .gitignore steht und die Versionen sonst je Rechner auseinanderlaufen.
    # 3.4.0 ist der aktuelle Stand.
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "3.4.0"
    }
    # Liefert den Typ local_file; damit schreibt outputs.tf das Inventory.
    local = {
      source  = "hashicorp/local"
      version = "2.5.3"
    }
  }
}
