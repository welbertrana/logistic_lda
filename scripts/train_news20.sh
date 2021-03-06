export PYTHONPATH=.:$PYTHONPATH

mkdir -p models/news20

python3 scripts/train.py \
  --dataset "data/news20/news20_train/" \
  --model_dir "models/news20/" \
  --initial_learning_rate 0.001 \
  --batch_size 256 \
  --embedding "one_hot" \
  --cache \
  --overwrite
