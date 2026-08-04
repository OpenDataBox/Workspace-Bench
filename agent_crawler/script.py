
import os


def export_folders_only(startpath, output_file):
    # 使用 errors='replace' 处理潜在的编码问题

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w', encoding='utf-8', errors='replace') as f:
        f.write(f"# 文件系统目录结构 (仅文件夹) \n")
        f.write(f"# 根目录: {os.path.abspath(startpath)}\n\n")

        for root, dirs, files in os.walk(startpath):
            # 排除根目录自身显示（可选）
            if root == startpath:
                f.write(f"- **[Root]**\n")
                continue

            # 计算相对于起始路径的深度，用于缩进
            level = root.replace(startpath, '').count(os.sep)
            indent = '  ' * level

            # 获取当前文件夹名称
            folder_name = os.path.basename(root)

            # 写入文件夹条目
            f.write(f"{indent}- **{folder_name}/**\n")


if __name__ == "__main__":
    # 1. 在这里修改你想扫描的目标文件夹
    target_dir = "/home/weizheng/RIPBench文件扩充/产品情景/chanpin/"

    # 2. 在这里修改你想保存 .md 文件的位置（也可以加上文件名）
    # 如果你也想把它存在 D:\xiangmu 目录下，就这样写：
    output_md = "//home/weizheng/RIPBench文件扩充/产品情景/文件树.md"
    export_folders_only(target_dir, output_md)
    print(f"导出完成！仅记录了文件夹结构。")
    print(f"结果已保存至: {output_md}")
