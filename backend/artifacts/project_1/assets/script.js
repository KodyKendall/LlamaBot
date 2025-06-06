// Wait for the DOM to be fully loaded
document.addEventListener('DOMContentLoaded', function() {
    const heading = document.querySelector('h1');
    const paragraph = document.querySelector('p');
    
    // Add click interaction to the heading
    heading.addEventListener('click', function() {
        const colors = ['#2c3e50', '#e74c3c', '#3498db', '#2ecc71', '#f39c12', '#9b59b6'];
        const randomColor = colors[Math.floor(Math.random() * colors.length)];
        this.style.color = randomColor;
        this.style.transform = 'scale(1.05)';
        
        // Reset transform after animation
        setTimeout(() => {
            this.style.transform = 'scale(1)';
        }, 200);
    });
    
    // Add hover effect to paragraph
    paragraph.addEventListener('mouseenter', function() {
        this.style.transform = 'translateY(-5px)';
        this.style.transition = 'transform 0.3s ease';
    });
    
    paragraph.addEventListener('mouseleave', function() {
        this.style.transform = 'translateY(0)';
    });
    
    // Dynamic text content
    const messages = [
        "This is a test page.",
        "Welcome to our simple website!",
        "Click the heading above to change its color!",
        "Hover over this text to see it move!",
        "JavaScript makes pages interactive!"
    ];
    
    let messageIndex = 0;
    
    // Change paragraph text every 3 seconds
    setInterval(function() {
        paragraph.style.opacity = '0';
        
        setTimeout(() => {
            messageIndex = (messageIndex + 1) % messages.length;
            paragraph.textContent = messages[messageIndex];
            paragraph.style.opacity = '1';
        }, 300);
    }, 3000);
    
    // Add smooth transition for opacity changes
    paragraph.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    
    // Add keyboard interaction
    document.addEventListener('keydown', function(event) {
        if (event.code === 'Space') {
            event.preventDefault();
            
            // Create a sparkle effect
            createSparkle(event);
            
            // Change heading text temporarily
            const originalText = heading.textContent;
            heading.textContent = '✨ Hello, Universe! ✨';
            
            setTimeout(() => {
                heading.textContent = originalText;
            }, 1500);
        }
    });
    
    // Sparkle effect function
    function createSparkle(event) {
        const sparkle = document.createElement('div');
        sparkle.innerHTML = '✨';
        sparkle.style.position = 'fixed';
        sparkle.style.left = '50%';
        sparkle.style.top = '50%';
        sparkle.style.transform = 'translate(-50%, -50%)';
        sparkle.style.fontSize = '2rem';
        sparkle.style.pointerEvents = 'none';
        sparkle.style.zIndex = '1000';
        sparkle.style.animation = 'sparkleAnimation 1s ease-out forwards';
        
        document.body.appendChild(sparkle);
        
        // Remove sparkle after animation
        setTimeout(() => {
            document.body.removeChild(sparkle);
        }, 1000);
    }
    
    // Add CSS animation for sparkles
    const style = document.createElement('style');
    style.textContent = `
        @keyframes sparkleAnimation {
            0% {
                opacity: 1;
                transform: translate(-50%, -50%) scale(0.5);
            }
            50% {
                opacity: 1;
                transform: translate(-50%, -50%) scale(1.5);
            }
            100% {
                opacity: 0;
                transform: translate(-50%, -50%) scale(0.5) translateY(-50px);
            }
        }
    `;
    document.head.appendChild(style);
    
    // Console welcome message
    console.log('🎉 Welcome to the interactive Hello World page!');
    console.log('💡 Try clicking the heading or pressing the spacebar!');
});
