# 1. Login + pick subscription
az login
az account set --subscription "6fe73089-e5dd-45b6-b59f-389bf4d6dfd1"

# 2. Variables
$rg        = "test-fpe-rg"
$loc       = "westus3"
$stor      = "stfpe$(Get-Random -Maximum 99999)"      # globally unique, lowercase
$func      = "func-fpe-$(Get-Random -Maximum 99999)"  # globally unique
$container = "deploymentpackage"
$vnet      = "vnet-fpe"
$snetPe    = "snet-pe"      # subnet hosting Private Endpoints
$snetFunc  = "snet-func"    # delegated subnet for Function App VNet integration

az group create -n $rg -l $loc

# 3. VNet + two subnets (PE subnet + delegated Function App subnet)
az network vnet create -g $rg -n $vnet -l $loc `
  --address-prefix 10.50.0.0/16 `
  --subnet-name $snetPe --subnet-prefix 10.50.1.0/24
az network vnet subnet update -g $rg --vnet-name $vnet -n $snetPe `
  --disable-private-endpoint-network-policies true
az network vnet subnet create -g $rg --vnet-name $vnet -n $snetFunc `
  --address-prefixes 10.50.2.0/24 `
  --delegations Microsoft.App/environments

# 4. Storage account: shared keys DISABLED, public network DISABLED by default.
#    Temporary client-IP allow lets us bootstrap the deployment container over
#    the public endpoint; we revoke it in the final step.
$myIp = (Invoke-RestMethod https://api.ipify.org)
az storage account create -n $stor -g $rg -l $loc `
  --sku Standard_LRS `
  --allow-shared-key-access false `
  --public-network-access Enabled `
  --default-action Deny `
  --bypass AzureServices

az storage account network-rule add -g $rg --account-name $stor --ip-address $myIp

$storId = az storage account show -n $stor -g $rg --query id -o tsv

# 5. Private Endpoints + Private DNS for blob, queue, table (Functions needs all three)
foreach ($svc in @("blob","queue","table")) {
  $zone = "privatelink.$svc.core.windows.net"
  az network private-dns zone create -g $rg -n $zone | Out-Null
  az network private-dns link vnet create -g $rg -n "$svc-link" `
    --zone-name $zone --virtual-network $vnet --registration-enabled false | Out-Null
  az network private-endpoint create -g $rg -n "pe-$stor-$svc" -l $loc `
    --vnet-name $vnet --subnet $snetPe `
    --private-connection-resource-id $storId `
    --group-id $svc --connection-name "pe-$stor-$svc-conn" | Out-Null
  az network private-endpoint dns-zone-group create -g $rg `
    --endpoint-name "pe-$stor-$svc" --name "default" `
    --private-dns-zone $zone --zone-name $svc | Out-Null
}

# 6. Pre-create the deployment container using YOUR Entra identity (AAD data plane)
$me = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $me --role "Storage Blob Data Owner" --scope $storId
Write-Host "Waiting 45s for role assignment to propagate..."
Start-Sleep -Seconds 45
az storage container create --name $container --account-name $stor --auth-mode login

# 7. Create the Flex Consumption Function App with VNet integration + system-assigned MI.
#    Flex stores the deployment package as a blob (no SMB file share, no shared key).
#    NOTE: Flex Consumption uses --flexconsumption-location (do NOT also pass -l/--location).
az functionapp create `
  -g $rg -n $func `
  --runtime python --runtime-version 3.11 `
  --flexconsumption-location $loc `
  --storage-account $stor `
  --deployment-storage-container-name $container `
  --deployment-storage-auth-type SystemAssignedIdentity `
  --vnet $vnet --subnet $snetFunc `
  --assign-identity '[system]'

# Force all outbound traffic through the VNet so storage is reached via Private Endpoints
az functionapp config appsettings set -g $rg -n $func --settings WEBSITE_VNET_ROUTE_ALL=1

# 8. Grant the Function App MI blob/queue/table access on the storage account
$funcMi = az functionapp identity show -g $rg -n $func --query principalId -o tsv
foreach ($role in @("Storage Blob Data Owner","Storage Queue Data Contributor","Storage Table Data Contributor")) {
  az role assignment create --assignee $funcMi --role $role --scope $storId
}

# 9. Switch AzureWebJobsStorage to identity-based (remove the shared-key connection string)
az functionapp config appsettings set -g $rg -n $func --settings `
  AzureWebJobsStorage__accountName=$stor `
  AzureWebJobsStorage__credential=managedidentity
az functionapp config appsettings delete -g $rg -n $func --setting-names AzureWebJobsStorage

# 10. App settings.
#     PoC: FPE_KEY/FPE_TWEAK as plain settings (no Key Vault dependency).
#       FPE_KEY:   32 bytes hex (AES-256)  -> 64 hex chars
#       FPE_TWEAK:  7 bytes hex (FF3-1)    -> 14 hex chars
#     Flex Consumption manages worker concurrency, so omit
#     FUNCTIONS_WORKER_PROCESS_COUNT / PYTHON_THREADPOOL_THREAD_COUNT.
#     EnableWorkerIndexing is REQUIRED for the Python v2 (@app.route) decorator model
#     so the host discovers functions defined in function_app.py.
$bytesKey = New-Object byte[] 32
$bytesTwk = New-Object byte[] 7
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytesKey)
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytesTwk)
$fpeKey   = ([System.BitConverter]::ToString($bytesKey)) -replace '-',''
$fpeTweak = ([System.BitConverter]::ToString($bytesTwk)) -replace '-',''
az functionapp config appsettings set -g $rg -n $func --settings `
  AzureWebJobsFeatureFlags=EnableWorkerIndexing `
  FPE_KEY=$fpeKey `
  FPE_TWEAK=$fpeTweak `
  FPE_MAX_BATCH_SIZE=10000

# 11. Publish the code.
#     Flex Consumption needs a Linux Python 3.11 build, but `func ... publish --python`
#     from Windows ships Windows wheels and the host fails to load them. Build the
#     dependencies in WSL Ubuntu (Python 3.11 from the deadsnakes PPA) and publish
#     with --no-build so the local .python_packages/ folder is used as-is.
#
#     One-time WSL setup:
#       sudo add-apt-repository -y ppa:deadsnakes/ppa
#       sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3.11-distutils
#
#     Each deploy:
#       cd /mnt/c/Users/<you>/Code/test-anonyimization-function/azure_function
#       rm -rf .python_packages
#       python3.11 -m pip install --upgrade pip
#       python3.11 -m pip install --target=.python_packages/lib/site-packages -r requirements.txt
#       func azure functionapp publish <func-name> --python --no-build
#
#     The publish still uses the storage account's public endpoint via the
#     temporary IP allow rule; future re-deploys must run from inside the VNet
#     (e.g. a jumpbox or self-hosted DevOps agent) once step 13 has locked it down.

# 12. Get endpoint + a function key (use functionKeys.default; per-function
#     `function keys list` returns NotFound on Flex Consumption).
$endpoint = "https://$func.azurewebsites.net/api/fpe"
$key = az functionapp keys list -g $rg -n $func --query "functionKeys.default" -o tsv
"endpoint: $endpoint"
"key:      $key"
"FPE_KEY (save this!):   $fpeKey"
"FPE_TWEAK (save this!): $fpeTweak"

# 13. Lock the storage account down: revoke client IP, disable public network entirely.
#     From now on, the Function App reaches storage only over the Private Endpoints.
#     To re-deploy code later from your laptop, temporarily run:
#       az storage account update -n $stor -g $rg --public-network-access Enabled --default-action Deny
#       az storage account network-rule add -g $rg --account-name $stor --ip-address <your-ip>
az storage account network-rule remove -g $rg --account-name $stor --ip-address $myIp
az storage account update -n $stor -g $rg --public-network-access Disabled