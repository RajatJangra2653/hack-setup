$token = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$h = @{Authorization = "Bearer $token"; "Content-Type" = "application/json"}
$cmd = @'
ls -la src/onedrive_provisioner/onedrive/ 2>&1
echo '---PY---'
python --version 2>&1
echo '---PIP LIST---'
pip list 2>&1 | grep -iE 'msal|flask|gunicorn'
echo '---IMPORT---'
python -u -c "import sys; sys.path.insert(0, 'src'); import app; print('OK', app.app)" 2>&1
echo '---DONE---'
'@
$body = @{ command = $cmd; dir = "/home/site/wwwroot" } | ConvertTo-Json
$r = Invoke-RestMethod -Uri "https://onedrive-provisioner-app.scm.azurewebsites.net/api/command" -Method POST -Headers $h -Body $body
"EXIT: $($r.ExitCode)"
"---OUTPUT---"
$r.Output
"---ERROR---"
$r.Error
