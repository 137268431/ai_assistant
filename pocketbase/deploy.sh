#!/bin/bash
set -euo pipefail

# ====================== 配置区 ======================
REMOTE_HOST="root@206.119.171.136"
REMOTE_DIR="/opt/pocketbase"
LOCAL_PATH1="/Users/lzwglory/glory/ai_assistant/pocketbase"
LOCAL_PATH2="/Users/lzwglory/ai_assistant/pocketbase"
# ====================================================

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

clear
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}          PocketBase 一键部署脚本            ${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""

# 选择本地路径
echo -e "${YELLOW}📂 请选择本地路径：${NC}"
echo "1) $LOCAL_PATH1"
echo "2) $LOCAL_PATH2"
read -p "请输入数字 [1-2]: " path_choice

case $path_choice in
    1) SELECTED_LOCAL_PATH="$LOCAL_PATH1" ;;
    2) SELECTED_LOCAL_PATH="$LOCAL_PATH2" ;;
    *) echo -e "${RED}❌ 输入错误${NC}"; exit 1 ;;
esac

if [ ! -d "$SELECTED_LOCAL_PATH" ]; then
    echo -e "${RED}❌ 路径不存在：$SELECTED_LOCAL_PATH${NC}"
    exit 1
fi
echo -e "${GREEN}✅ 已选择：$SELECTED_LOCAL_PATH${NC}"
echo ""

# 选择上传内容
echo -e "${YELLOW}🚀 请选择上传内容：${NC}"
echo "1) 全部上传 (pb_public + pb_hooks)"
echo "2) 仅上传 pb_public"
echo "3) 仅上传 pb_hooks"
read -p "请输入数字 [1-3]: " upload_choice
echo ""

# 上传函数：只清空文件夹内文件，保留文件夹本身，且只传内容不嵌套
upload_dir() {
    local local_sub=$1
    local remote_sub=$2

    echo -e "${YELLOW}🗑️ 清空远程 $remote_sub 内所有文件...${NC}"
    ssh $REMOTE_HOST "rm -rf $REMOTE_DIR/$remote_sub/* $REMOTE_DIR/$remote_sub/.* 2>/dev/null || true"

    echo -e "${YELLOW}📤 上传新文件到 $remote_sub...${NC}"
    # 关键修正：只传子目录下的所有内容，不包含子目录本身
    scp -rp "$SELECTED_LOCAL_PATH/$local_sub/"* "$REMOTE_HOST:$REMOTE_DIR/$remote_sub/"
}

# 执行上传
echo -e "${GREEN}🚀 开始部署...${NC}"
case $upload_choice in
    1)
        upload_dir "pb_public" "pb_public"
        upload_dir "pb_hooks" "pb_hooks"
        ;;
    2)
        upload_dir "pb_public" "pb_public"
        ;;
    3)
        upload_dir "pb_hooks" "pb_hooks"
        ;;
    *)
        echo -e "${RED}❌ 输入错误${NC}"; exit 1
        ;;
esac

echo ""
echo -e "${YELLOW}🔄 重启 PocketBase 服务...${NC}"
ssh $REMOTE_HOST "systemctl restart pocketbase"

echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}🎉 部署完成！文件已直接放在目标目录下，无嵌套${NC}"
echo -e "${GREEN}=============================================${NC}"