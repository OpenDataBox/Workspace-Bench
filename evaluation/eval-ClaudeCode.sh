nohup /home/tangzirui/miniconda3/bin/python \
    -u src/agent_runner.py \
    --run-config runs/ClaudeCode--GLM-5.1--Test-Rubrics-Checked.yaml \
    > logs/ClaudeCode-GLM.log 2>&1 &
PID=$!
echo $PID >> logs/ClaudeCode-GLM.log
echo "ClaudeCode-GLM started with PID: $PID"
echo "ClaudeCode-GLM started with PID: $PID" >> logs/RUN-PIDs.log

nohup /home/tangzirui/miniconda3/bin/python \
    -u src/agent_runner.py \
    --run-config runs/ClaudeCode--Kimi-2.5--Test-Rubrics-Checked.yaml \
    > logs/ClaudeCode-Kimi.log 2>&1 &
PID=$!
echo $PID >> logs/ClaudeCode-Kimi.log
echo "ClaudeCode-Kimi started with PID: $PID" >> logs/RUN-PIDs.log
echo "ClaudeCode-Kimi started with PID: $PID"

nohup /home/tangzirui/miniconda3/bin/python \
    -u src/agent_runner.py \
    --run-config runs/ClaudeCode--MiniMax-M2.7--Test-Rubrics-Checked.yaml \
    > logs/ClaudeCode-MiniMax.log 2>&1 & 
PID=$!
echo $PID >> logs/ClaudeCode-MiniMax.log
echo "ClaudeCode-MiniMax started with PID: $PID" >> logs/RUN-PIDs.log
echo "ClaudeCode-MiniMax started with PID: $PID"

# nohup /home/tangzirui/miniconda3/bin/python \
#     -u src/agent_runner.py \
#     --run-config runs/ClaudeCode--Seed-2.0-Lite--Test-Rubrics-Checked.yaml \
#     > logs/ClaudeCode-Seed-Lite.log 2>&1 & 
# PID=$!
# echo $PID >> logs/ClaudeCode-Seed-Lite.log
# echo "ClaudeCode-Seed-Lite started with PID: $PID" >> logs/RUN-PIDs.log
# echo "ClaudeCode-Seed-Lite started with PID: $PID"

