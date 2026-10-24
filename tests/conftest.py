import os

# 测试与宿主机运行状态隔离: 网页切拼接纸型会写 airprint-spool/media-override.txt,
# 不能让该文件影响用例结果 (job_model 的 media-size 取决于它)
os.environ['MIJIA_MEDIA_OVERRIDE_FILE'] = os.devnull
