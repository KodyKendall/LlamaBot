const canvas = document.getElementById('gameCanvas');
const ctx = canvas.getContext('2d');
const gridSize = 20;
const tileCount = canvas.width / gridSize;
let snake = [{ x: 10, y: 10 }];
let velocity = { x: 0, y: 0 };
let food = { x: 15, y: 15 };
let score = 0;
let gameInterval;

function draw() {
  // Move snake
  const head = { x: snake[0].x + velocity.x, y: snake[0].y + velocity.y };
  snake.unshift(head);

  // Check food collision
  if (head.x === food.x && head.y === food.y) {
    score++;
    document.getElementById('score').innerText = `Score: ${score}`;
    placeFood();
  } else {
    snake.pop();
  }

  // Check wall collision
  if (head.x < 0 || head.x >= tileCount || head.y < 0 || head.y >= tileCount) {
    gameOver();
    return;
  }

  // Check self collision
  for (let i = 1; i < snake.length; i++) {
    if (snake[i].x === head.x && snake[i].y === head.y) {
      gameOver();
      return;
    }
  }

  // Draw background
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Draw snake
  ctx.fillStyle = '#0f0';
  snake.forEach(segment => {
    ctx.fillRect(segment.x * gridSize, segment.y * gridSize, gridSize - 2, gridSize - 2);
  });

  // Draw food
  ctx.fillStyle = '#f00';
  ctx.fillRect(food.x * gridSize, food.y * gridSize, gridSize - 2, gridSize - 2);
}

function placeFood() {
  food.x = Math.floor(Math.random() * tileCount);
  food.y = Math.floor(Math.random() * tileCount);
  // Ensure food not on snake
  snake.forEach(segment => {
    if (segment.x === food.x && segment.y === food.y) {
      placeFood();
    }
  });
}

function gameOver() {
  clearInterval(gameInterval);
  alert(`Game Over! Your score: ${score}`);
}

function startGame() {
  snake = [{ x: 10, y: 10 }];
  velocity = { x: 0, y: 0 };
  score = 0;
  document.getElementById('score').innerText = `Score: ${score}`;
  placeFood();
  clearInterval(gameInterval);
  gameInterval = setInterval(draw, 100);
}

document.addEventListener('keydown', e => {
  switch (e.key) {
    case 'ArrowUp':
      if (velocity.y === 0) velocity = { x: 0, y: -1 };
      break;
    case 'ArrowDown':
      if (velocity.y === 0) velocity = { x: 0, y: 1 };
      break;
    case 'ArrowLeft':
      if (velocity.x === 0) velocity = { x: -1, y: 0 };
      break;
    case 'ArrowRight':
      if (velocity.x === 0) velocity = { x: 1, y: 0 };
      break;
    case 'Enter':
      startGame();
      break;
  }
});

// Auto-start
startGame();
