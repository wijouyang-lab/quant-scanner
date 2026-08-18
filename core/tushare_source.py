name: A-Share Daily Scan

on:
  schedule:
    # 这里的 cron 表达式使用 UTC 时间。
    # 00:30 UTC 对应北京时间早上 08:30，周一到周五运行
    - cron: '30 0 * * 1-5'
  
  # workflow_dispatch 允许你在 GitHub 网页端手动点击按钮来触发运行，非常适合刚部署完的测试
  workflow_dispatch:

jobs:
  run-scan:
    runs-on: ubuntu-latest

    steps:
    - name: Checkout repository
      uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.10'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        # 安装你代码里用到的核心依赖库
        pip install pandas numpy tushare anthropic yfinance

    - name: Run A-Share Pre-market Scan
      # 这里的 env 将你在 GitHub Secrets 里配置的值提取出来，注入到代码运行环境中
      env:
        TUSHARE_TOKEN: ${{ secrets.TUSHARE_TOKEN }}
        CLAWSOCKET_API_KEY: ${{ secrets.CLAWSOCKET_API_KEY }}
        CLAWSOCKET_BASE_URL: ${{ secrets.CLAWSOCKET_BASE_URL }}
        EMAIL_ACCOUNT: ${{ secrets.EMAIL_ACCOUNT }}
        EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
        TARGET_EMAILS: ${{ secrets.TARGET_EMAILS }}
      run: |
        python scan_ashare.py
