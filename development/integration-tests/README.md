# Life Link 跨项目集成测试

本目录只放同时依赖 `pc-dashboard` 与 `central-server` 的端到端测试，避免任何一个独立项目为了运行自身测试而导入另一个项目。

运行：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" -m unittest discover -s development/integration-tests -p "test_*.py"
```
