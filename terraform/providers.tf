# auth_url und Region sind für die ganze Gruppe gleich und stehen deshalb
# fest hier; nur die persönlichen Zugangsdaten kommen aus Variablen und damit
# aus der lokalen terraform.tfvars. Application Credential statt Nutzername
# und Passwort, weil es projektgebunden und jederzeit widerrufbar ist.
provider "openstack" {
  auth_url                      = "https://newstack.dhbw.cloud:5000"
  region                        = "DHBW-MA"
  application_credential_id     = var.application_credential_id
  application_credential_secret = var.application_credential_secret
}
