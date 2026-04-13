#!/bin/bash

echo "======================================"
echo "      Claude Agents Launcher"
echo "======================================"
echo ""
echo "Select CLI type:"
echo "  1) claude"
echo "  2) codex"
echo "  3) droid"
echo ""
read -p "> " cli_type

case $cli_type in
    1) cli="claude" ;;
    2) cli="codex" ;;
    3) cli="droid" ;;
    *)
        echo "Invalid selection. Exiting."
        exit 1
        ;;
esac

echo ""
echo "Select working directory:"
echo "  1) /Users/lzwglory/trading/trading_flight"
echo "  2) /Users/lzwglory/glory/ai_assistant"
echo "  3) /Users/lzwglory"
echo "  4) /Users/lzwglory/glory"
echo ""
read -p "> " path_selection

case $path_selection in
    1) cd /Users/lzwglory/trading/trading_flight ;;
    2) cd /Users/lzwglory/glory/ai_assistant ;;
    3) cd /Users/lzwglory ;;
    4) cd /Users/lzwglory/glory ;;
    *)
        echo "Invalid selection. Exiting."
        exit 1
        ;;
esac

echo ""
echo "Available models for $cli:"
if [ "$cli" = "claude" ]; then
    echo "  1) qwen3-max-2026-01-23"
    echo "  2) claude-opus-4-6-thinking"
    echo "  3) claude-opus-4-6-20260205"
    echo "  4) opus"
    echo "  5) sonnet"
    echo "  6) MiniMax-M2.5"
    echo "  7) MiniMax-M2.7"
    echo ""
    echo "Select ONE model to start:"
    read -p "> " selection

    case $selection in
        1) model="qwen3-max-2026-01-23" ;;
        2) model="claude-opus-4-6-thinking" ;;
        3) model="claude-opus-4-6-20260205" ;;
        4) model="opus" ;;
        5) model="sonnet" ;;
        6) model="MiniMax-M2.5" ;;
        7) model="MiniMax-M2.7" ;;
        *)
            echo "Invalid selection. Exiting."
            exit 1
            ;;
    esac
    model_arg="--model $model"
elif [ "$cli" = "codex" ]; then
    echo "  1) gpt-5.4"
    echo ""
    echo "Select ONE model to start:"
    read -p "> " selection

    case $selection in
        1) model="gpt-5.4" ;;
        *)
            echo "Invalid selection. Exiting."
            exit 1
            ;;
    esac
    model_arg="--model $model"
else
    echo "  (直接回车跳过，不指定 model)"
    echo "  1) qwen3-max-2026-01-23"
    echo "  2) claude-opus-4-6-thinking"
    echo "  3) claude-opus-4-5-thinking"
    echo "  4) opus"
    echo "  5) sonnet"
    echo "  6) MiniMax-M2.5"
    echo "  7) MiniMax-M2.7"
    echo ""
    echo "Select model or回车跳过:"
    read -p "> " selection

    case $selection in
        1) model="qwen3-max-2026-01-23" ;;
        2) model="claude-opus-4-6-thinking" ;;
        3) model="claude-opus-4-5-thinking" ;;
        4) model="opus" ;;
        5) model="sonnet" ;;
        6) model="MiniMax-M2.5" ;;
        7) model="MiniMax-M2.7" ;;
        *) model="" ;;
    esac
    if [ -n "$model" ]; then
        model_arg="--model $model"
    else
        model_arg=""
    fi
fi

echo ""
echo "======================================"
echo "Starting: $cli $model_arg"
echo "======================================"
echo ""

$cli $model_arg

# 等待用户按回车键再关闭
read -p "Press Enter to exit..."
