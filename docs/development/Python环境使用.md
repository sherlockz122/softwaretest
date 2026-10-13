# Python 环境使用

项目要求 Python 3.11，根目录 `.python-version` 用于向支持它的工具声明版本。该文件本身不会改变 Windows PATH，也不会让普通终端在进入目录后自动切换解释器。

## 项目环境

项目依赖安装在根目录 `.venv` 中，与系统 Python 的第三方包隔离。`.venv` 已被 Git 忽略，不上传、不复制给其他机器；其他成员应使用自己的 Python 3.11 重新创建环境。

创建时必须指定已经确认版本的 Python 3.11 解释器。不能在默认 Python 为 3.13 的终端直接执行 `python -m venv .venv`，否则创建的也是 3.13 环境。后续依赖清单和锁文件在工程框架阶段加入。

## 日常使用（PowerShell）

在项目根目录激活环境：

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -c "import sys; print(sys.executable)"
python -m pip --version
```

应显示 Python 3.11.x，解释器路径应指向本项目 `.venv\Scripts\python.exe`。依赖安装使用 `python -m pip`，以确保 pip 属于当前解释器。

退出当前环境：

```powershell
deactivate
```

激活只调整当前终端进程的 PATH；其他终端及系统/用户的持久化 PATH 不受影响。退出后恢复激活前的命令查找路径。

无需激活，也可以明确选择解释器：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
```

如果激活脚本受执行策略限制，优先使用上述直接调用方式，不为此放宽整台机器的执行策略。

## 编辑器与自动化

编辑器为当前项目选择 `.venv\Scripts\python.exe` 即可；选择只适用于项目配置，不必修改系统默认 Python。自动化脚本同样可以明确使用该路径。

默认 `python`、Python Launcher 的 `py`、虚拟环境、编辑器解释器设置是不同的选择机制。排查时以 `sys.executable` 和 `sys.version` 为准，不能仅凭 PATH 条目或文件夹名字推断实际版本。

Python 本体和 `.venv` 的基础解释器有关联，不要在使用环境时移动或删除基础 Python。基础解释器迁移或升级时应按项目依赖记录重建 `.venv`。

参考：[Python 3.11 venv 文档](https://docs.python.org/3.11/library/venv.html)。
